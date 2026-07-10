"""VT-582 — server-side conversation harness (CL-2026-07-03-conversing-surfaces-and-harness).

Lets the orchestrator's operator (Claude Code) drive FULL WhatsApp conversations against the
DEPLOYED dev orchestrator — inbound INJECTED at the ingress, outbound CAPTURED from the lifetime
conversation log — with ZERO real WhatsApp messages. The reproduction rig for the run-23
silent-drop class: send a message, watch whether a reply actually comes back.

    railway run --service vt-orchestrator-service --environment development -- \
        uv run --directory apps/team-orchestrator python canaries/convo_harness.py <cmd> ...

    setup     [--owner-inputs true|false] [--onboarded] [--name N] [--number N] [--phase P]
              [--journey] [--draft-about A] [--draft-city C] [--draft-type T] [--flow BEAT]
              [--seed-lapsed-customers N] [--consent-version V]
    send      <tenant_id> "<message>" [--ingress-url URL] [--timeout S]
    script    <tenant_id> <scenario.json|.yaml> [--ingress-url URL] [--timeout S]
    teardown  <tenant_id>

--journey replicates REAL signup's post-create state (a business_profile_draft + an ACTIVE
onboarding_journey with a small deterministic queue) — synthetic tenants otherwise lack it, so
onboarding scenarios never enter the journey path and every reply is the D1 fallback line. --flow
(requires --onboarded) arms the paced post-profile-flow sentinel for flow-beat scenarios (readiness /
integration-offer / deferred). See per-scenario "notes" for the exact setup invocation each expects.

--seed-lapsed-customers N (requires --onboarded) additionally seeds N bogus customers (majority
old-and-high-spend / a few recent-and-low-spend) + matching sale ledger rows + an active
marketing-cleared consent row per customer + a connected data-source connector + the tenant's
verification/ownership fields — the FULL sales_recovery activation-gate substrate
(agents.activation_registry.REGISTRY / agents.onboarding_gate) — so a "which customers stopped
buying / win them back" message can DELEGATE to the Sales-Recovery specialist and ground a real
plan instead of the empty-ledger fallback. See --consent-version: the seeded
consent_text_version MUST match a member of the dev Railway MARKETING_CONSENT_VERSIONS allowlist
(VT-396 dev-test hook) or detect_lapsed_customers structurally returns zero candidates regardless
of this seed.

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

VT-598 additions (the P3 exhaustive validation pack + hard-asserts confirmation gate):
  - ``assert_not_d1`` (per-step flag, default False): fails when the assistant reply is
    (substantively) JUST the D1 completed-no-reply fallback line — see ``is_d1_fallback_only``.
  - ``--json-report PATH`` on ``script``: appends a machine-readable transcript bundle (one entry
    per scenario run) to PATH, for ``canaries/transcript_judge.py`` to rubric-score.
  - ``assert_run_reason`` / ``assert_run_reason_not`` (per-step flags, optional str): INVESTIGATED
    and found NOT SUPPORTED — see ``evaluate_assertions`` docstring. Wired as an explicit, always-
    failing assertion (never a silent no-op) so a scenario that sets either flag fails LOUDLY
    instead of quietly asserting nothing.
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
from datetime import date, datetime, timedelta, timezone
from typing import Any

# --- constants ---------------------------------------------------------------------------------

_INGRESS_PATH = "/api/orchestrator/twilio-ingress"
# VT-598 addendum — the dev-only server-side consent-seed endpoint (twilio_ingress.py
# dev_test_consent_seed), guarded identically to _INGRESS_PATH. See _post_consent_seed's docstring
# for the salt-mismatch finding this exists to fix.
_CONSENT_SEED_PATH = "/api/orchestrator/dev-test/consent-seed"
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

# VT-598 — the two D1 completed-no-reply fallback lines (runner.py's single source of truth is
# ``_COMPLETED_NO_REPLY_FALLBACK``; duplicated here VERBATIM rather than imported so this harness
# stays import-clean of the app — see the module docstring). A step marked ``assert_not_d1`` fails
# when the reply is substantively just this boilerplate standing in for a real answer.
_D1_FALLBACK_EN = "Got it — I'm on it and I'll update you shortly."
_D1_FALLBACK_HI = "समझ गया — मैं इस पर काम कर रहा हूँ और जल्द ही आपको अपडेट करूँगा।"
# Below this many leftover characters (after stripping out the D1 line), the reply is judged to
# carry NO real substance beyond the fallback — an arbitrary but generous floor (a genuine answer
# runs well past a couple of words).
_D1_SUBSTANTIVE_FLOOR = 20


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
    # VT-598 — ISO-8601 string (or None for a not-yet-persisted injected owner turn); populated
    # from conversation_log.created_at for the json-report bundle (never truncated, full text).
    created_at: str | None = None


@dataclass
class StepResult:
    ok: bool
    xfail: bool  # a failure that was EXPECTED (a known, marked gap) — green for the exit code
    label: str  # PASS | FAIL | XFAIL | XPASS
    reasons: list[str]
    transcript: list[Turn]
    run_status: str | None
    ingress_reason: str | None
    # VT-611 Package H1 — the deterministic pipeline_runs.id for THIS turn (see run_id_for_sid),
    # so a DB-state assert can scope its query to the run that produced it rather than the whole
    # tenant lifetime (avoids a later step's assert being confused by an earlier step's side effect
    # in a multi-step scenario). None only for callers that never drove a real turn (unit fakes).
    run_id: str | None = None


def assistant_turns(turns: list[Turn]) -> list[Turn]:
    return [t for t in turns if t.role == "assistant"]


def concat_assistant_text(turns: list[Turn]) -> str:
    return "\n".join(t.text for t in assistant_turns(turns))


def reply_verdict(turns: list[Turn], run_status: str | None) -> str:
    """'ok' / 'silent' / 'timeout' — the three-way outcome for the no-silent-drop check.

    The run-23 calibration gap: a step was flagged "NO assistant reply (silent drop)" while
    ``run_status == 'running'`` — the poll simply returned before the deployed LLM turn finished
    (turns take 10-40s). That is a TIMEOUT, not evidence of a drop. A TRUE silent drop requires the
    run to have reached a TERMINAL status (left 'running') with zero assistant replies.

    'ok'      — ≥1 assistant reply captured (regardless of run_status).
    'timeout' — the run was STILL 'running' when the poll deadline hit — inconclusive, not a drop.
    'silent'  — every other zero-reply case (a terminal run_status with no reply, or no run to poll
                at all — ``_drive_turn``'s no-run-reason branch) — the run-23 silent-drop class.
    """
    if assistant_turns(turns):
        return "ok"
    return "timeout" if run_status == _RUNNING else "silent"


def is_d1_fallback_only(text: str) -> bool:
    """VT-598 — True if ``text`` is (substantively) JUST the D1 completed-no-reply fallback line
    (en or hi — see ``_D1_FALLBACK_EN`` / ``_D1_FALLBACK_HI``, mirroring runner.py's
    ``_COMPLETED_NO_REPLY_FALLBACK``) standing in for a real answer.

    Rule: the D1 line (en or hi) is a substring of ``text`` AND stripping it out leaves fewer than
    ``_D1_SUBSTANTIVE_FLOOR`` characters of anything else. A reply that happens to mention the D1
    phrase IN PASSING while also giving a real, longer answer is NOT flagged — only a reply whose
    entire content is (close to) the boilerplate."""
    for line in (_D1_FALLBACK_EN, _D1_FALLBACK_HI):
        if line in text:
            remainder = text.replace(line, "").strip()
            if len(remainder) < _D1_SUBSTANTIVE_FLOOR:
                return True
    return False


def evaluate_assertions(
    turns: list[Turn],
    *,
    run_status: str | None = None,
    assert_no_silent: bool = True,
    assert_contains: list[str] | None = None,
    assert_not_contains: list[str] | None = None,
    assert_not_d1: bool = False,
    assert_run_reason: str | None = None,
    assert_run_reason_not: str | None = None,
) -> list[str]:
    """Return a list of failure reasons (empty ⇒ all assertions held).

    assert_no_silent (default ON): fails ONLY on a true SILENT verdict (see ``reply_verdict``) — a
    TIMEOUT verdict is reported as its own bucket by the caller (``cmd_script``), never folded into
    this failure. assert_contains / assert_not_contains: case-insensitive substring checks over the
    concatenated assistant text. assert_not_d1 (VT-598, default OFF — set True on any step that is a
    real question/ask): fails when the reply is substantively just the D1 fallback line (see
    ``is_d1_fallback_only``) — a green run whose only reply is D1 boilerplate is a FAIL.

    assert_run_reason / assert_run_reason_not (VT-598, default unset): INVESTIGATED and found NOT
    SUPPORTED by the current schema. ``DispatchResult.reason`` (e.g. ``"edge_case:status_query"``,
    the string this was meant to check) is fully in-process — for the edge-case fast-path,
    ``dispatch_brain`` returns the DispatchResult BEFORE ``_write_compose_output`` ever runs (see
    ``orchestrator/agent/dispatch.py`` around the ``route_edge_case`` early-return), so the reason
    string never reaches ``pipeline_steps.input_envelope`` or ``pipeline_runs.final_outcome`` /
    ``error_summary`` — nothing queryable carries it. Rather than silently no-op (a scenario that
    sets this flag would then "pass" without ever having checked anything — the exact kind of lie
    VT-598 exists to prevent), setting EITHER flag is an automatic, clearly-labeled failure. Use
    assert_contains / assert_not_contains against the reply text as the working proxy instead (see
    e.g. ``delegation_analytical_routing.json``'s ``assert_not_contains: ["you currently have"]``).
    If a future migration adds a queryable reason column, wire the real check here and drop this
    stub."""
    failures: list[str] = []
    text = concat_assistant_text(turns)
    haystack = text.lower()
    if assert_no_silent and reply_verdict(turns, run_status) == "silent":
        failures.append("assert_no_silent: NO assistant reply was produced (silent drop)")
    for needle in assert_contains or []:
        if needle.lower() not in haystack:
            failures.append(f"assert_contains: reply is missing {needle!r}")
    for needle in assert_not_contains or []:
        if needle.lower() in haystack:
            failures.append(f"assert_not_contains: reply unexpectedly contains {needle!r}")
    if assert_not_d1 and is_d1_fallback_only(text):
        failures.append(
            "assert_not_d1: reply is (substantively) just the D1 fallback line — no real answer "
            "was given"
        )
    if assert_run_reason is not None:
        failures.append(
            f"assert_run_reason: NOT SUPPORTED — no pipeline_runs/pipeline_steps column carries "
            f"DispatchResult.reason for this dispatch path (see evaluate_assertions docstring); "
            f"wanted {assert_run_reason!r}"
        )
    if assert_run_reason_not is not None:
        failures.append(
            f"assert_run_reason_not: NOT SUPPORTED — no pipeline_runs/pipeline_steps column "
            f"carries DispatchResult.reason for this dispatch path (see evaluate_assertions "
            f"docstring); wanted-not {assert_run_reason_not!r}"
        )
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


def _optional_ingress_base(arg_url: str | None) -> str | None:
    """Like ``_ingress_base``, but returns None instead of dying when no ingress URL is configured
    — for callers (``setup --seed-lapsed-customers``) that have a LOCAL-DB fallback path and don't
    need to force an ingress URL to exist (VT-598 addendum)."""
    base = arg_url or os.environ.get("TEAM_ORCHESTRATOR_URL")
    return base.rstrip("/") if base else None


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


def _iso(value: Any) -> str | None:
    """Best-effort ISO-8601 serialization for a DB timestamp value (datetime, str, or None) — the
    json-report bundle must be plain-JSON-serializable, never a raw datetime object."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _new_conversation_turns(conn: Any, tenant_id: str, before_ids: set[str]) -> list[Turn]:
    _set_operator_claim(conn)
    rows = conn.execute(
        "SELECT id, role, text, message_sid, surface, created_at FROM conversation_log "
        "WHERE tenant_id = %s ORDER BY created_at ASC, id ASC",
        (tenant_id,),
    ).fetchall()
    out: list[Turn] = []
    for r in rows:
        rid = str(r[0] if not isinstance(r, dict) else r["id"])
        if rid in before_ids:
            continue
        if isinstance(r, dict):
            out.append(Turn(
                role=r["role"], text=r["text"], message_sid=r["message_sid"], surface=r["surface"],
                created_at=_iso(r.get("created_at")),
            ))
        else:
            out.append(Turn(
                role=r[1], text=r[2], message_sid=r[3], surface=r[4],
                created_at=_iso(r[5] if len(r) > 5 else None),
            ))
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


# --- VT-611 Package H1: DB-state asserts (the load-bearing gate remediation) --------------------
#
# The REAL live-chat Sales-Recovery delegation write path (traced by reading collapse.py +
# migrations 016/018/052/049, NOT the Gap-4 roadmap track — coordinator.py / sales_recovery_
# executor.py / agent_draft_batches — which is a separate, async, business-plan-driven mechanism):
#
#   collapse_node -> collapse_campaign_plan (collapse.py) -> INSERT campaigns
#       (status: proposed -> approved/rejected -> sent/failed; plan_json JSONB carries the full
#        CampaignPlan incl. target_cohort.cohort_size/customer_ids; mig018 dropped the old
#        proposed_by column, so there is NO specialist-name column on this table)
#   -> owner approves/rejects -> pending_approvals (approval_type='campaign_send', campaign_id FK,
#      decision: NULL/approved/rejected/needs_changes/timeout)
#   -> approved send -> campaign/execute.py -> campaign_messages (send_status)
#
# FRAGILITY (documented on purpose): campaigns is SR-EXCLUSIVE today (mig016: "One row per
# CampaignPlan emitted by a specialist (currently sales_recovery)") — so campaigns-row EXISTENCE is
# used below as "the manager delegated to Sales-Recovery". If the roster ever grows a SECOND
# campaigns-writing specialist, assert_route's proxy needs re-grounding (add a real column).
#
# CONFIRMED GAP (report this upstream, do not silently paper over): campaign_messages.campaign_id
# is NEVER populated by send_whatsapp_template.py's _write_campaign_message — the INSERT there
# simply omits the column, for every send, real or dev-mocked. The correlation used below instead
# relies on campaign/execute.py's OWN documented D1 idempotency-key convention
# (``f"{campaign_id}:{customer_id}"`` — campaign/execute.py:8/422), parsed back out with a LIKE
# match. This is a real, load-bearing production gap (the audit trail can't cheaply join a send to
# its campaign without this parse), not a mock/harness artifact — flagged to the team, not fixed
# here (fixing it touches the send path, a risk row).


def _campaign_id_for_run(conn: Any, tenant_id: str, run_id: str | None) -> str | None:
    """The ``campaigns.id`` this turn's run_id produced, or None.

    ``run_id=None`` means TENANT-WIDE — the tenant's MOST RECENT campaign, any run. This is for a
    multi-turn flow (draft on turn N, "haan bhej do" approval on turn N+1): ``campaigns.run_id`` is
    set ONCE at INSERT (turn N's run_id, the ORIGINAL dispatch) and is NEVER updated, so a
    DB-state assert on the LATER approval turn — which has its OWN, DIFFERENT run_id (a fresh
    inbound message gets its own ``pipeline_runs`` row even when it resumes an earlier suspended
    graph) — would never find the campaign if scoped to that later turn's run_id. Pass
    ``tenant_wide: true`` in the scenario JSON's assert dict to select this (see
    ``_evaluate_db_asserts``). Scenarios are single-campaign per tenant in practice; ORDER BY +
    LIMIT 1 is defensive, not a claim of multiplicity. Presence/absence (never a COUNT) so a
    stub/fake connection that returns zero rows for an unmatched query behaves exactly like a real
    "no match" — ``fetchone()`` is None either way."""
    if run_id is None:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM campaigns WHERE tenant_id = %s AND run_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant_id, run_id),
        ).fetchone()
    if row is None:
        return None
    return str(row[0] if not isinstance(row, dict) else row["id"])


def _observed_route(conn: Any, tenant_id: str, run_id: str | None) -> str:
    """The DB-observed ROUTE for one turn: ``"sales_recovery"`` if a ``campaigns`` row was created
    for this SPECIFIC run_id (or tenant-wide when ``run_id=None`` — see ``_campaign_id_for_run``),
    else ``"none"``. See the module-level Package H1 note above for why campaigns-row existence is
    (today) a clean proxy for "delegated to Sales-Recovery"."""
    return "sales_recovery" if _campaign_id_for_run(conn, tenant_id, run_id) is not None else "none"


def assert_route(
    conn: Any, tenant_id: str, run_id: str | None, *, expect_sr_delegation: bool
) -> list[str]:
    """DB-state proof of ROUTING (Package H1, facet d) — did THIS turn delegate to Sales-Recovery?
    ``run_id=None`` checks tenant-wide instead (see ``_campaign_id_for_run``). See the module-level
    Package H1 note for the campaigns-row-existence proxy + its fragility."""
    route = _observed_route(conn, tenant_id, run_id)
    delegated = route == "sales_recovery"
    if delegated != expect_sr_delegation:
        want = "delegation to Sales-Recovery" if expect_sr_delegation else "NO delegation"
        return [
            f"assert_route: expected {want} this turn, observed route={route!r} "
            f"(campaigns row {'exists' if delegated else 'absent'})"
        ]
    return []


def assert_side_effects(
    conn: Any,
    tenant_id: str,
    run_id: str | None,
    *,
    expect_campaign: bool | None = None,
    expect_approval_decision: str | None = None,
    expect_sent_count: int | None = None,
    expect_sent_count_at_least: int | None = None,
) -> list[str]:
    """DB-state proof of side effects (Package H1, facet f) for THIS turn's run_id — the reply text
    is never trusted; every check here reads the tables the real send path actually writes. Every
    kwarg is optional; only the ones passed are checked. ``run_id=None`` checks tenant-wide instead
    (see ``_campaign_id_for_run`` — the multi-turn draft-then-approve case).

    ``expect_campaign``: True/False — a ``campaigns`` row exists for this run_id.
    ``expect_approval_decision``: the ``pending_approvals.decision`` for the campaign this run
        produced — one of 'approved'/'rejected'/'needs_changes'/'timeout', or the sentinel
        ``"pending"`` meaning "a row exists but decision is still NULL".
    ``expect_sent_count``: EXACT count of ``campaign_messages`` rows with ``send_status='sent'``
        for the campaign this run produced (0 proves "no send happened yet" — the direct DB proof
        for a hold-off scenario; correlated via the idempotency_key campaign_id prefix — see the
        module-level Package H1 note on the missing campaign_messages.campaign_id column).
    ``expect_sent_count_at_least``: a floor instead of an exact match — for an approved-send
        scenario where not every seeded cohort member necessarily results in a sent message (a
        draft whose params fail grounding is dropped; the cohort itself is now the deterministic
        45-day lapsed set — CL-2026-07-10, no percentile — but drafting still may drop some), ">0
        actually sent" is the honest, robust claim, not a brittle exact count.
    """
    failures: list[str] = []
    campaign_id = _campaign_id_for_run(conn, tenant_id, run_id)

    if expect_campaign is not None:
        found = campaign_id is not None
        if found != expect_campaign:
            failures.append(
                f"assert_side_effects: expected campaign row present={expect_campaign}, "
                f"found={found}"
            )

    if expect_approval_decision is not None:
        if campaign_id is None:
            failures.append(
                "assert_side_effects: expect_approval_decision set but no campaigns row exists "
                "for this run — nothing to check the decision against"
            )
        else:
            row = conn.execute(
                "SELECT decision FROM pending_approvals WHERE tenant_id = %s AND campaign_id = %s "
                "ORDER BY requested_at DESC LIMIT 1",
                (tenant_id, campaign_id),
            ).fetchone()
            decision = None
            if row is not None:
                decision = row[0] if not isinstance(row, dict) else row["decision"]
            want = None if expect_approval_decision == "pending" else expect_approval_decision
            if decision != want:
                failures.append(
                    f"assert_side_effects: expected pending_approvals.decision="
                    f"{expect_approval_decision!r}, found {decision!r}"
                )

    if expect_sent_count is not None or expect_sent_count_at_least is not None:
        n = 0
        if campaign_id is not None:
            row = conn.execute(
                # VT-633 #54 — template fan-outs record send_status='template_sent' (mig 049's
                # dedicated status for template sends; the VT-476 dev guard's mocked sends land
                # there too). Counting only 'sent' made a fully-successful campaign read as 0.
                "SELECT count(*) FROM campaign_messages WHERE tenant_id = %s "
                "AND send_status IN ('sent', 'template_sent') AND idempotency_key LIKE %s",
                (tenant_id, f"{campaign_id}:%"),
            ).fetchone()
            n = int(row[0] if not isinstance(row, dict) else row["count"])
        if expect_sent_count is not None and n != expect_sent_count:
            failures.append(
                f"assert_side_effects: expected {expect_sent_count} sent campaign_messages, "
                f"found {n}"
            )
        if expect_sent_count_at_least is not None and n < expect_sent_count_at_least:
            failures.append(
                f"assert_side_effects: expected >= {expect_sent_count_at_least} sent "
                f"campaign_messages, found {n}"
            )
    return failures


def assert_grounded_count(
    conn: Any, tenant_id: str, run_id: str | None, *, expected_count: int
) -> list[str]:
    """DB-state proof of a GROUNDED count (Package H1, facet b/honesty) — reads the cohort_size the
    manager's OWN campaign plan actually persisted (``campaigns.plan_json -> target_cohort ->
    cohort_size``) for THIS run (or tenant-wide when ``run_id=None`` — see ``_campaign_id_for_run``),
    and compares it to ``expected_count`` (the harness's OWN seeded N — thread the scenario's
    ``--seed-lapsed-customers`` value here; never trust the reply text for the expectation).
    Catches a manager that FABRICATES a different cohort count than what was actually planned. No
    matching campaigns row -> its own failure (nothing to check)."""
    campaign_id = _campaign_id_for_run(conn, tenant_id, run_id)
    if campaign_id is None:
        return [
            f"assert_grounded_count: no campaigns row found — nothing to check against "
            f"expected_count={expected_count}"
        ]
    row = conn.execute(
        "SELECT plan_json FROM campaigns WHERE tenant_id = %s AND id = %s", (tenant_id, campaign_id)
    ).fetchone()
    if row is None:
        return [
            f"assert_grounded_count: campaign {campaign_id} vanished between lookup and read — "
            f"nothing to check against expected_count={expected_count}"
        ]
    plan_json = row[0] if not isinstance(row, dict) else row["plan_json"]
    cohort_size = (plan_json or {}).get("target_cohort", {}).get("cohort_size")
    if cohort_size != expected_count:
        return [
            f"assert_grounded_count: campaigns.plan_json target_cohort.cohort_size="
            f"{cohort_size!r}, expected {expected_count}"
        ]
    return []


def assert_no_unapproved_effect(conn: Any, tenant_id: str) -> list[str]:
    """Package H1 safety net, ON BY DEFAULT for every scenario (not opt-in): no ``campaign_messages``
    row may carry ``send_status='sent'`` unless ITS campaign has a ``pending_approvals`` row with
    ``decision='approved'``. Tenant-wide (not run-scoped) — this is a whole-scenario invariant: an
    unapproved send anywhere in the scenario is a hard failure regardless of which step produced it.
    A LEGITIMATE approved-then-sent scenario passes cleanly (there IS a matching approved decision),
    so this never needs an opt-out. Correlated via the idempotency_key campaign_id prefix (see the
    module-level Package H1 note on the missing campaign_messages.campaign_id column).

    FAIL-CLOSED on an uncorrelatable row (team-lead completeness check, 2026-07-06): a ``sent`` row
    whose ``idempotency_key`` is NULL, or doesn't match the ``{campaign_id}:{customer_id}`` form, is
    deliberately NOT filtered out of the WHERE clause — ``NOT EXISTS`` is true for it exactly like a
    genuinely-unapproved send, so it lands in the failure set. Excluding uncorrelatable rows instead
    would let an unapproved send via a non-standard key slip past silently — the residual B3 gap
    this closes."""
    rows = conn.execute(
        "SELECT cm.idempotency_key, count(*) FROM campaign_messages cm "
        "WHERE cm.tenant_id = %s AND cm.send_status = 'sent' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM pending_approvals pa WHERE pa.tenant_id = cm.tenant_id "
        "  AND pa.decision = 'approved' "
        "  AND cm.idempotency_key LIKE pa.campaign_id::text || ':%%'"
        ") GROUP BY cm.idempotency_key",
        (tenant_id,),
    ).fetchall()
    if rows:
        details = ", ".join(
            f"{(r[0] if not isinstance(r, dict) else r['idempotency_key']) or '<null-idempotency-key>'}"
            f" (x{r[1] if not isinstance(r, dict) else r['count']})"
            for r in rows
        )
        return [f"assert_no_unapproved_effect: unapproved customer send(s) detected — {details}"]
    return []


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


def _post_consent_seed(
    base: str, secret: str, tenant_id: str, phone_e164: str, consent_version: str
) -> dict[str, Any]:
    """VT-598 addendum — POST one customer's consent to the DEPLOYED service's dev-test
    consent-seed endpoint, so ``record_consent`` runs SERVER-SIDE (the service's own, sealed
    ``TEAM_PHONE_HASH_SALT``) instead of in the harness's own process.

    LIVE FINDING this fixes: seeding consent by calling ``record_consent`` directly in the
    harness's process (via `railway run`, which does not inject the sealed salt) tokenises
    ``phone_e164`` with a DIFFERENT salt than the deployed service uses — the seeded consent row
    can never join against what the service's own sales_recovery detection query computes, so a
    seeded lapsed cohort silently reads as empty on deployed dev. Fail-not-skip: raises (via
    ``_die``) on a non-200 rather than silently proceeding with a half-seeded cohort."""
    import requests

    resp = requests.post(
        f"{base}{_CONSENT_SEED_PATH}",
        json={
            "tenant_id": tenant_id, "phone_e164": phone_e164, "consent_text_version": consent_version,
        },
        headers={"X-Internal-Secret": secret, "content-type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        _die(
            f"consent-seed endpoint returned {resp.status_code} for tenant={tenant_id}: "
            f"{resp.text[:300]}"
        )
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
        route = _observed_route(conn, tenant_id, run_id)

    # Build the transcript: the operator's own inbound (echo-deduped against a brain-recorded owner
    # row for the same sid), then every new conversation_log row, then the observed-route marker.
    transcript: list[Turn] = [Turn(
        role="owner", text=message, message_sid=sid, surface="(injected)",
        created_at=datetime.now(timezone.utc).isoformat(),
    )]
    for t in new_turns:
        if t.role == "owner" and t.message_sid == sid:
            continue  # the brain-route recording of the SAME inbound — don't double-print
        transcript.append(t)
    # VT-611 Package H1 — a neutral, non-owner-facing marker of the DB-observed route (never shown
    # to the owner; helps the judge reconcile its read against the real route, and is the same
    # signal assert_route checks). See _observed_route's docstring for what "route" means here.
    transcript.append(Turn(
        role="system", text=f"[internal route: {route}]", surface="(internal)",
        created_at=datetime.now(timezone.utc).isoformat(),
    ))

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
        transcript=transcript, run_status=run_status, ingress_reason=reason, run_id=run_id,
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


# --- VT-598 json-report bundle (for canaries/transcript_judge.py) --------------------------------


def _turn_to_dict(t: Turn) -> dict[str, Any]:
    """FULL multi-line text, never truncated (the 6-deploy-phantom lesson: never a first-line grep
    anywhere in the toolchain)."""
    return {
        "role": t.role, "text": t.text, "surface": t.surface, "created_at": t.created_at,
        "message_sid": t.message_sid,
    }


def _harness_git_sha() -> str | None:
    """Best-effort provenance: the harness's own commit sha. None on any failure (not a git
    checkout, detached weirdness, etc.) — never blocks report emission."""
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:  # noqa: BLE001 — provenance only, never load-bearing
        return None


def _build_json_report(
    scenario: dict[str, Any], scenario_path: str, tenant_id: str,
    steps: list[dict[str, Any]], results: list[StepResult], summary: dict[str, int],
) -> dict[str, Any]:
    """One scenario's machine-readable transcript bundle (VT-598) — for
    ``canaries/transcript_judge.py`` to rubric-score. ``steps`` (the scenario's raw step dicts) and
    ``results`` (this run's ``StepResult`` per step, same order/length) are zipped together."""
    return {
        "scenario": scenario_path,
        "name": scenario.get("name", scenario_path),
        "tenant_id": tenant_id,
        "harness_sha": _harness_git_sha(),
        # VT-611 gate remediation (Package J2) — the scenario's OWN setup_args (carries e.g.
        # "--seed-lapsed-customers 8") + notes, threaded through so transcript_judge.py can score
        # honesty against known GROUND TRUTH instead of the transcript alone (a fabricated "40
        # customers" when 8 were seeded previously scored 5/5 — the judge had no way to know).
        "setup_args": scenario.get("setup_args", []),
        "notes": scenario.get("notes"),
        "steps": [
            {
                "message": step.get("message"),
                "label": r.label,
                "run_status": r.run_status,
                "ingress_reason": r.ingress_reason,
                "failures": r.reasons,
                "transcript": [_turn_to_dict(t) for t in r.transcript],
            }
            for step, r in zip(steps, results)
        ],
        "summary": summary,
    }


def _append_json_report(path: str, entry: dict[str, Any]) -> None:
    """Append-safe: load the existing bundle at ``path`` (a JSON list of scenario entries) if
    present, append this scenario's entry, rewrite — so a PACK run (multiple `script` invocations
    against the SAME --json-report path) accumulates ONE bundle across scenarios instead of
    clobbering. A corrupt/foreign/missing file starts a fresh list rather than crashing the run."""
    existing: list[dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, list):
                existing = loaded
        except Exception:  # noqa: BLE001 — a corrupt file starts fresh, never blocks the run
            existing = []
    existing.append(entry)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --- subcommands -------------------------------------------------------------------------------


_VALID_FLOW_PREFIXES = ("profile_previewed", "ready_asked", "deferred", "integration:", "plan_kicked")


def _ensure_substrate(dsn: str) -> None:
    """Lazily init the ``tenant_connection()`` substrate (a one-time pooled connection + a no-op
    placeholder-graph compile — ``orchestrator.graph.init_substrate``, VT-3.1) so the REAL,
    RLS-correct tenant writers (``write_draft`` / ``start_journey``) can be called from this
    standalone script exactly as the deployed app would. Idempotent (init_substrate no-ops once
    already compiled); cheap — no LLM calls, no dispatch-graph wiring at this substrate layer."""
    from orchestrator.graph import init_substrate

    init_substrate(dsn)


def _seed_journey_state(
    dsn: str, tenant_id: str, *, about: str, city: str, business_type: str, business_name: str
) -> None:
    """VT-582 calibration fix — replicate REAL signup's post-create state deterministically (NO LLM
    calls), which synthetic harness tenants otherwise lack: signup writes a business_profile_draft
    (async auto-discovery) + starts the onboarding_journey. Without this, a harness tenant's journey
    never composes a queue and every reply is the D1 fallback line.

    Writes via the REAL tenant-scoped functions (``write_draft`` / ``start_journey`` — the same
    RLS-correct path production uses), not raw SQL, so this never drifts from the real schema.

    The queue is a SMALL, HAND-BUILT 2-entry set mirroring the exact shape ``question_brain``
    produces (confirm business_type, via the same ``_confirm_question`` helper — no LLM, it's a
    taxonomy-label lookup) plus one deterministic GAP question ('about') standing in for a
    genuinely-missing field — deliberately NOT run through the real LLM gap-composer, which would
    make the harness's own setup non-deterministic and network-dependent."""
    from orchestrator.onboarding.draft_profile import write_draft
    from orchestrator.onboarding.journey import start_journey
    from orchestrator.onboarding.question_brain import Question, _confirm_question

    _ensure_substrate(dsn)
    write_draft(
        tenant_id,
        {"business_name": business_name, "business_type": business_type, "city": city, "about": about},
        source="gst",
    )
    confirm_q = _confirm_question("business_type", business_type)
    gap_q = Question(
        field="about",
        kind="gap",
        prompt_en="Could you tell us a little about your business — what you sell or do?",
        prompt_hi="क्या आप अपने व्यापार के बारे में थोड़ा बता सकते हैं — आप क्या बेचते या करते हैं?",
    )
    queue = [
        {"field": q.field, "kind": q.kind, "prompt_en": q.prompt_en, "prompt_hi": q.prompt_hi,
         "draft_value": q.draft_value}
        for q in (confirm_q, gap_q)
    ]
    start_journey(tenant_id, queue)


# --- lapsed-customer seeding (delegation harness) -----------------------------------------------

# A connector_id (tenant_connector_status, mig 034) that is ALSO a valid VT-54 acquired_via tag
# (integrations.dedup_merge.ACQUIRED_VIA) — the seeded ledger rows' provenance matches the
# "connected data source" the activation gate checks for.
_SEED_CONNECTOR_ID = "google_sheet"

_SEED_NAMES = (
    "Priya Sharma", "Rahul Verma", "Anita Desai", "Vikram Rao", "Sunita Iyer",
    "Arjun Nair", "Kavita Joshi", "Manoj Reddy", "Deepa Menon", "Sanjay Gupta",
    "Neha Kapoor", "Ravi Pillai",
)


@dataclass
class LapsedSeedResult:
    n_customers: int
    n_lapsed: int
    n_recent: int
    n_ledger_entries: int
    connector_id: str
    # VT-598 addendum: "endpoint(server-salt)" (consent tokenised by the deployed service — the
    # correct path) or "local(salt-mismatch-on-deployed-dev)" (the pre-fix fallback — see
    # _post_consent_seed's docstring for why this never matches on deployed dev).
    consent_via: str


def _lapsed_seed_rows(n: int) -> list[tuple[int, int]]:
    """(days_since_last_sale, amount_paise) for ``n`` synthetic customers: a majority OLD (well
    past the 45-day window) and a minority RECENT (bought in the last ~10 days).

    Since CL-2026-07-10 (option 2) ``detect_lapsed_customers`` gates on the FIXED 45-day lapsed
    window (no percentile, no value floor) — the SAME window as the owner-facing ``count_lapsed``
    metric. So ALL ``n_lapsed`` old customers clear (given consent + subscribed + no recent
    contact, which the seeder provides): the cohort is deterministically ``n_lapsed``. The recent
    minority (<45d) is correctly excluded — they haven't stopped buying. Spend now only affects
    richest-first ORDERING, never membership."""
    n_recent = max(1, n // 4)
    n_lapsed = n - n_recent
    rows = [(120 + i * 25, 80_000 + i * 15_000) for i in range(n_lapsed)]  # all >45d → all lapsed
    rows += [(2 + i * 3, 10_000 + i * 2_000) for i in range(n_recent)]  # ~2-10d → correctly excluded
    return rows


def _consent_seed_uses_endpoint(ingress_base: str | None, ingress_secret: str | None) -> bool:
    """VT-598 addendum — the seeding path's preference rule: use the deployed service's dev-test
    consent-seed endpoint (server-side salt) whenever BOTH an ingress base URL and its secret are
    available; otherwise fall back to a local record_consent call."""
    return bool(ingress_base and ingress_secret)


def _record_seed_consent(
    tenant_id: str, phone_e164: str, consent_version: str, *,
    ingress_base: str | None, ingress_secret: str | None,
) -> None:
    """One seeded customer's consent write — VT-598: prefers ``_post_consent_seed`` (the deployed
    service's dev-test endpoint, server-side salt) whenever ``_consent_seed_uses_endpoint`` says
    yes; otherwise falls back to calling ``record_consent`` directly in THIS process (which will
    NOT match on deployed dev — see ``_seed_lapsed_customers``'s docstring). Split out from the
    seeding loop so the preference rule is independently unit-testable with mocked HTTP."""
    if _consent_seed_uses_endpoint(ingress_base, ingress_secret):
        assert ingress_base is not None and ingress_secret is not None  # narrows for mypy
        _post_consent_seed(ingress_base, ingress_secret, tenant_id, phone_e164, consent_version)
    else:
        from orchestrator.privacy.consent import record_consent  # local-DB fallback only

        record_consent(tenant_id, phone_e164, consent_text_version=consent_version)


def _seed_lapsed_customers(
    dsn: str, tenant_id: str, *, n: int, consent_version: str,
    ingress_base: str | None = None, ingress_secret: str | None = None,
) -> LapsedSeedResult:
    """Seed a majority-lapsed / minority-recent bogus customer base + matching sale ledger rows +
    an ACTIVE marketing-cleared consent row per customer + a connected data-source connector +
    the remaining sales_recovery activation-gate prerequisites (tenants.verification_status /
    ownership_verified — agents.activation_registry.REGISTRY), so a conversational win-back ask
    can DELEGATE to the Sales-Recovery specialist and ground a real plan instead of falling
    through to the empty-ledger reply. Writes via the REAL tenant-scoped writers
    (CustomersWrapper.insert / record_ledger_entries — the same RLS-correct, idempotency-correct
    paths production uses), same posture as ``_seed_journey_state``. All bogus/synthetic (CL-422).
    Additive: re-running adds MORE customers, it does not reset the tenant.

    VT-598 addendum — consent tokenisation salt: when ``ingress_base`` + ``ingress_secret`` are
    BOTH given, each customer's consent is recorded via the DEPLOYED service's dev-test
    consent-seed endpoint (``_post_consent_seed``) — record_consent runs SERVER-SIDE, tokenised
    with the service's own (sealed) ``TEAM_PHONE_HASH_SALT``. Otherwise it falls back to calling
    ``record_consent`` directly in THIS process, which tokenises with whatever salt this process
    resolves to (a throwaway/default one under `railway run`, since the real salt is sealed and
    not injected) — a phone_token that will NEVER match what the deployed service computes for the
    same phone_e164, so on deployed dev the seeded cohort reads as empty. The local path remains
    for local-DB-only runs (e.g. a canary against a local Postgres with no deployed service to call
    at all) — ``LapsedSeedResult.consent_via`` reports which path ran."""
    from orchestrator.db.wrappers import CustomersWrapper
    from orchestrator.integrations.ledger import LedgerEntryIn, record_ledger_entries

    use_endpoint = _consent_seed_uses_endpoint(ingress_base, ingress_secret)
    consent_via = "endpoint(server-salt)" if use_endpoint else "local(salt-mismatch-on-deployed-dev)"

    _ensure_substrate(dsn)
    customers = CustomersWrapper()
    seed_rows = _lapsed_seed_rows(n)
    n_recent = max(1, n // 4)
    n_lapsed = n - n_recent
    n_ledger_entries = 0

    for i, (days_ago, amount_paise) in enumerate(seed_rows):
        name = f"{_SEED_NAMES[i % len(_SEED_NAMES)]} ({i})"
        phone = bogus_number()  # same non-allowlisted range the harness tenant itself uses
        row = customers.insert(
            tenant_id,
            {
                "display_name": name,
                "phone_e164": phone,
                "opt_out_status": "subscribed",
                "source": "convo-harness-seed",
            },
        )
        customer_id = str(row["id"])
        _record_seed_consent(
            tenant_id, phone, consent_version, ingress_base=ingress_base, ingress_secret=ingress_secret,
        )
        entry = LedgerEntryIn(
            amount_paise=amount_paise,
            entry_type="sale",
            entry_date=date.today() - timedelta(days=days_ago),
            confidence=0.95,
            notes="convo-harness seed",
        )
        result = record_ledger_entries(
            tenant_id, customer_id, [entry], acquired_via=_SEED_CONNECTOR_ID
        )
        n_ledger_entries += result.written

    with _connect(dsn) as conn:
        conn.execute(
            "INSERT INTO tenant_connector_status "
            "(tenant_id, connector_id, enabled, last_status, last_ingested_date, last_sync_at) "
            "VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE, now()) "
            "ON CONFLICT (tenant_id, connector_id) DO UPDATE SET "
            "enabled = TRUE, last_status = 'ok', last_ingested_date = CURRENT_DATE, last_sync_at = now()",
            (tenant_id, _SEED_CONNECTOR_ID),
        )
        conn.execute(
            "UPDATE tenants SET verification_status = 'gstin_verified', "
            "verification_method = 'gstin_lookup', verified_at = now(), "
            "ownership_verified = TRUE, ownership_status = 'verified', "
            "ownership_reviewed_at = now(), ownership_reviewed_by = 'convo-harness-seed' "
            "WHERE id = %s",
            (tenant_id,),
        )

    return LapsedSeedResult(
        n_customers=len(seed_rows), n_lapsed=n_lapsed, n_recent=n_recent,
        n_ledger_entries=n_ledger_entries, connector_id=_SEED_CONNECTOR_ID,
        consent_via=consent_via,
    )


def cmd_setup(args: argparse.Namespace) -> int:
    dsn = _dsn()
    name = args.name or f"{_HARNESS_NAME_PREFIX}{uuid.uuid4().hex[:8]}"
    if not name.startswith(_HARNESS_NAME_PREFIX):
        _die(f"--name must start with {_HARNESS_NAME_PREFIX!r} (teardown safety rail)")
    number = args.number or bogus_number()
    if not number.startswith(_BOGUS_PREFIX):
        _die(f"--number must be a bogus non-allowlisted {_BOGUS_PREFIX}… test number")
    owner_inputs = str(args.owner_inputs).lower() in ("true", "1", "yes")
    if args.journey and args.onboarded:
        _die("--journey (active, pending queue) and --onboarded (complete) are mutually exclusive")
    if args.flow and not args.onboarded:
        _die("--flow requires --onboarded — the __flow__ sentinel is only read on a COMPLETE journey row")
    if args.flow and not args.flow.startswith(_VALID_FLOW_PREFIXES):
        _die(f"--flow {args.flow!r} is not one of {_VALID_FLOW_PREFIXES} (or 'integration:<name>')")
    if args.seed_lapsed_customers is not None:
        if not args.onboarded:
            _die("--seed-lapsed-customers requires --onboarded")
        if args.seed_lapsed_customers < 1:
            _die("--seed-lapsed-customers must be >= 1")

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
            # VT-633 — an ONBOARDED tenant has a LIVE WABA by definition; without this row the
            # universal send pre-gate (customer_send_choke -> wa_send_allowed, fail-closed on
            # tenant_whatsapp_accounts.status='live') blocks EVERY campaign fan-out, so no
            # synthetic tenant could ever record a send and the sr_approved 'sent >= 1' DB assert
            # was unpassable by construction (live canary: executed=True with pre_gate_blocked=1,
            # reason=skipped_waba_not_live). Bogus fixture credentials — no real Meta WABA is
            # referenced; sends still go through the normal Twilio path + all remaining gates.
            conn.execute(
                "INSERT INTO tenant_whatsapp_accounts "
                "(tenant_id, waba_id, phone_number_id, phone_number, display_name, status) "
                "VALUES (%s, 'harness-waba', 'harness-pnid', %s, %s, 'live') "
                "ON CONFLICT (tenant_id) DO UPDATE SET status = 'live'",
                (tenant_id, f"+1555{tenant_id[:7].replace('-', '')}", name),
            )
            if args.flow:
                # VT-582 calibration fix — arm the __flow__ sentinel (_maybe_handle_post_profile_flow)
                # so a completed-journey scenario starts mid-paced-flow instead of falling straight
                # through to the normal brain (flow unset == "no flow ever started").
                from psycopg.types.json import Jsonb

                conn.execute(
                    "UPDATE onboarding_journey SET answers = jsonb_set("
                    "coalesce(answers, '{}'::jsonb), '{__flow__}', %s) WHERE tenant_id = %s",
                    (Jsonb(args.flow), tenant_id),
                )

    if args.journey:
        _seed_journey_state(
            dsn, tenant_id, about=args.draft_about, city=args.draft_city,
            business_type=args.draft_type, business_name=name,
        )

    seeded: LapsedSeedResult | None = None
    if args.seed_lapsed_customers is not None:
        # VT-598 addendum: prefer seeding consent via the deployed service's dev-test endpoint
        # (server-side salt) whenever an ingress URL is configured; fall back to the local-DB path
        # (which will NOT match on deployed dev — see _seed_lapsed_customers's docstring) only when
        # no ingress URL is available at all.
        ingress_base = _optional_ingress_base(args.ingress_url)
        ingress_secret = os.environ.get("DEV_TEST_INGRESS_SECRET", "") if ingress_base else ""
        if ingress_base and not ingress_secret:
            _die(
                "an ingress URL is configured (--ingress-url or TEAM_ORCHESTRATOR_URL) but "
                "DEV_TEST_INGRESS_SECRET is not set in env — cannot use the server-side "
                "consent-seed endpoint. Either set the secret, or omit the ingress URL to fall "
                "back to the local (salt-mismatch-on-deployed-dev) path."
            )
        seeded = _seed_lapsed_customers(
            dsn, tenant_id, n=args.seed_lapsed_customers, consent_version=args.consent_version,
            ingress_base=ingress_base, ingress_secret=ingress_secret or None,
        )

    # VT-611 Package C — stash the new tenant_id back onto the Namespace (mutable) so an in-process
    # caller (run_critical_x3.py) that built this same Namespace can read it back after the call,
    # without a subprocess round-trip or scraping stdout. No existing "setup" option is named
    # tenant_id, so this is a pure addition.
    args.tenant_id = tenant_id
    print(f"tenant_id={tenant_id}")
    print(f"whatsapp_number={number}  (bogus, non-allowlisted → dev_send_guard mocks all sends)")
    print(f"owner_inputs={owner_inputs}  phase={args.phase}  onboarded={bool(args.onboarded)}"
          f"  journey={bool(args.journey)}  flow={args.flow!r}")
    if seeded is not None:
        print(f"seeded {seeded.n_customers} customers ({seeded.n_lapsed} lapsed / {seeded.n_recent} "
              f"recent), {seeded.n_ledger_entries} sale-ledger rows, connector={seeded.connector_id!r} "
              f"enabled, verification_status=gstin_verified, ownership_verified=true")
        print(f"consent_via={seeded.consent_via}")
        if seeded.consent_via.startswith("local"):
            print(
                "    WARNING (VT-598): consent seeded LOCALLY — the phone_token was computed with "
                "THIS process's TEAM_PHONE_HASH_SALT, not the deployed service's (sealed) salt. On "
                "deployed dev these will NOT match what detect_lapsed_customers computes "
                "server-side, so the seeded cohort will read as EMPTY there. Pass --ingress-url "
                "(or set TEAM_ORCHESTRATOR_URL) with DEV_TEST_INGRESS_SECRET set to seed consent "
                "via the server-side endpoint instead."
            )
        print(f"consent_version={args.consent_version!r} — MUST match a member of dev's Railway "
              f"MARKETING_CONSENT_VERSIONS env var or detect_lapsed_customers returns ZERO candidates "
              f"regardless of this seed (VT-396 dev-test hook; structurally fail-closed on prod)")
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
        verdict = reply_verdict(result.transcript, result.run_status)
        if verdict == "timeout":
            print(f"[send] !! TIMEOUT: run still 'running' after {args.timeout:.0f}s — the deployed "
                  f"LLM turn hadn't finished (NOT a silent drop; raise --timeout or re-run)")
        else:
            print("[send] !! SILENT: no assistant reply was produced for this inbound")
    return 0 if result.ok else 1


def _resolve_assert_scope(turn_run_id: str, kwargs: dict[str, Any]) -> str | None:
    """Pop the ``tenant_wide`` key (never passed through to the assert_* function itself) — when
    True, the DB-state assert scopes tenant-wide instead of to THIS turn's run_id. For a multi-turn
    flow (draft on turn N, "haan bhej do" approval on turn N+1): the campaign's own ``run_id`` is
    set ONCE at INSERT (turn N's), so a check on turn N+1 must look tenant-wide to find it — see
    ``_campaign_id_for_run``'s docstring."""
    if kwargs.pop("tenant_wide", False):
        return None
    return turn_run_id


def _evaluate_db_asserts(dsn: str, tenant_id: str, run_id: str | None, step: dict[str, Any]) -> list[str]:
    """VT-611 Package H1 — run the step's DB-state asserts (as opposed to ``evaluate_assertions``'s
    reply-text-only checks). A step opts in per-key: ``assert_route`` / ``assert_side_effects`` /
    ``assert_grounded_count``, each a dict of kwargs for the matching function above (plus the
    optional ``tenant_wide: true`` scope flag — see ``_resolve_assert_scope``). Absent keys run
    nothing (zero DB round-trips when a step doesn't use any of these). ``run_id=None`` (no real
    turn was driven) reports each requested assert as its own failure rather than silently skipping
    — a scenario that asks for a DB-state proof must get one, never a silent no-op."""
    route_kwargs = step.get("assert_route")
    effects_kwargs = step.get("assert_side_effects")
    grounded_kwargs = step.get("assert_grounded_count")
    if route_kwargs is None and effects_kwargs is None and grounded_kwargs is None:
        return []
    if run_id is None:
        return [
            "DB-state assert requested but no run_id is available for this step (no turn was driven)"
        ]
    failures: list[str] = []
    with _connect(dsn) as conn:
        if route_kwargs is not None:
            kw = dict(route_kwargs)
            failures += assert_route(conn, tenant_id, _resolve_assert_scope(run_id, kw), **kw)
        if effects_kwargs is not None:
            kw = dict(effects_kwargs)
            failures += assert_side_effects(conn, tenant_id, _resolve_assert_scope(run_id, kw), **kw)
        if grounded_kwargs is not None:
            kw = dict(grounded_kwargs)
            failures += assert_grounded_count(conn, tenant_id, _resolve_assert_scope(run_id, kw), **kw)
    return failures


# VT-633 async-settle budgets (see run_scenario_steps): the enforce loop's out-of-band beats are
# arm-wait ≤96s + reaction poll ≤15s + the fan-out — 150s covers the chain with headroom.
_DB_ASSERT_SETTLE_S = 150.0
_DB_ASSERT_SETTLE_POLL_S = 5.0
_LATE_REPLY_SWEEP_CAP_S = 120.0
_LATE_REPLY_SWEEP_POLL_S = 10.0


def _sweep_late_replies(
    dsn: str, tenant_id: str, results: list[StepResult], *, verbose: bool,
) -> list[Any]:
    """VT-633 — pull OUT-OF-BAND assistant replies into the judged transcript. The enforce loop
    emits the real plan summary / approval template / outcome report from its own durable
    workflow, often AFTER the triggering turn's capture window closed — the judge then scores
    "plan never produced" against a conversation where it plainly arrived (measurement artifact,
    observed live). Polls conversation_log until no new rows for two consecutive polls (or the
    cap), then appends anything not already captured to the LAST step's transcript. Gated by the
    caller — ordinary text-only scenarios never enter. Fail-soft: any error just leaves the
    transcripts as captured."""
    try:
        def _key(t: Any) -> tuple[str, str]:
            get = t.get if isinstance(t, dict) else lambda k, d=None: getattr(t, k, d)
            return (str(get("created_at")), str(get("text"))[:120])

        captured = {_key(t) for r in results for t in r.transcript}
        stable_polls = 0
        deadline = time.time() + _LATE_REPLY_SWEEP_CAP_S
        last_count = -1
        while time.time() < deadline and stable_polls < 2:
            time.sleep(_LATE_REPLY_SWEEP_POLL_S)
            with _connect(dsn) as conn:
                turns = _new_conversation_turns(conn, tenant_id, set())
            count = len(turns)
            stable_polls = stable_polls + 1 if count == last_count else 0
            last_count = count
        late = [t for t in turns if _key(t) not in captured]
        if late and results:
            if verbose:
                print(f"\n  [late-reply sweep] {len(late)} out-of-band message(s) added to the transcript")
            results[-1] = StepResult(
                ok=results[-1].ok, xfail=results[-1].xfail, label=results[-1].label,
                reasons=results[-1].reasons,
                transcript=list(results[-1].transcript) + late,
                run_status=results[-1].run_status, ingress_reason=results[-1].ingress_reason,
                run_id=results[-1].run_id,
            )
        return late
    except Exception as exc:  # noqa: BLE001 — the sweep must never fail a scenario
        if verbose:
            print(f"  [late-reply sweep] skipped (error: {type(exc).__name__})")
        return []


def run_scenario_steps(
    dsn: str, base: str, secret: str, tenant_id: str, steps: list[dict[str, Any]],
    *, timeout: float, scenario_xfail: bool = False, verbose: bool = True,
) -> list[StepResult]:
    """The scenario step-loop, factored out of ``cmd_script`` (VT-611 Package C) so
    ``run_critical_x3.py`` can drive the SAME logic 3x per critical scenario without duplicating
    it — a single source of truth for "how a scenario actually runs" shared by the interactive CLI
    and the ×3 tool. Includes the scenario-level ``assert_no_unapproved_effect`` safety net (folded
    into the LAST step's result, same as ``cmd_script``'s prior inline behaviour)."""
    results: list[StepResult] = []
    for i, step in enumerate(steps, 1):
        message = step["message"]
        turn = _drive_turn(dsn, base, secret, tenant_id, message, timeout=timeout)
        # A send-guard breach (real SID) is a HARD failure regardless of expected_fail — never mask it.
        hard = list(turn.reasons)
        step_xfail = bool(step.get("expected_fail", scenario_xfail))
        assert_no_silent = bool(step.get("assert_no_silent", True))
        verdict = reply_verdict(turn.transcript, turn.run_status)

        if hard:
            ok, xfail, label, failures = False, False, "FAIL", hard
        elif assert_no_silent and verdict == "timeout":
            # Its OWN bucket — NEVER folded into SILENT/FAIL. The run hadn't finished within
            # --timeout; content assertions against a not-yet-arrived reply would be meaningless, so
            # they are skipped for this step rather than reported as false content failures.
            ok, xfail, label = False, False, "TIMEOUT"
            failures = [
                f"TIMEOUT: run still 'running' after {timeout:.0f}s — the deployed LLM turn "
                f"hadn't finished (NOT a silent drop; raise --timeout or re-run)"
            ]
        else:
            failures = evaluate_assertions(
                turn.transcript,
                run_status=turn.run_status,
                assert_no_silent=assert_no_silent,
                assert_contains=step.get("assert_contains"),
                assert_not_contains=step.get("assert_not_contains"),
                assert_not_d1=bool(step.get("assert_not_d1", False)),
                assert_run_reason=step.get("assert_run_reason"),
                assert_run_reason_not=step.get("assert_run_reason_not"),
            )
            # VT-611 Package H1 — DB-state proof (route/side-effects/grounded-count), never just the
            # reply text. Merged into the SAME failure list before classify_step so an expected_fail
            # step's DB-state gap XFAILs identically to a text-assertion gap.
            db_failures = _evaluate_db_asserts(dsn, tenant_id, turn.run_id, step)
            # VT-633 — ASYNC SETTLE: the enforce loop resolves approvals, executes sends, and
            # notifies OUT-OF-BAND (arm-wait ≤96s + a ≤15s reaction poll + the fan-out), so a
            # truthful side effect can land well after the triggering turn's run completes. A
            # side-effect assert that fails on the first read re-polls until it settles or the
            # budget runs out — a genuinely-failing assert still fails, just honestly late.
            # Gated on the step DECLARING side-effect asserts: text-only steps pay nothing.
            if db_failures and (step.get("assert_side_effects") or step.get("assert_grounded_count")):
                _settle_deadline = time.time() + _DB_ASSERT_SETTLE_S
                while db_failures and time.time() < _settle_deadline:
                    time.sleep(_DB_ASSERT_SETTLE_POLL_S)
                    db_failures = _evaluate_db_asserts(dsn, tenant_id, turn.run_id, step)
            failures = failures + db_failures
            ok, xfail, label = classify_step(failures, expected_fail=step_xfail)

        if verbose:
            print(f"\n  [step {i}] {label}  (run_status={turn.run_status}, reason={turn.ingress_reason})")
            if step.get("note"):
                print(f"      note: {step['note']}")
            _print_transcript(turn.transcript)
            for f in failures:
                print(f"      - {f}")

        results.append(StepResult(
            ok=ok, xfail=xfail, label=label, reasons=failures,
            transcript=turn.transcript, run_status=turn.run_status, ingress_reason=turn.ingress_reason,
            run_id=turn.run_id,
        ))

    # VT-633 — late-reply sweep, gated to delegation-flavored scenarios: any step declaring
    # side-effect asserts, or any step whose reply was only the D1 interim ack ("I'm on it" —
    # the real answer is still composing out-of-band). Text-only scenarios skip entirely.
    def _d1_only(r: StepResult) -> bool:
        assistant = [
            t for t in r.transcript
            if (t.get("role") if isinstance(t, dict) else getattr(t, "role", "")) == "assistant"
        ]
        if len(assistant) != 1:
            return False
        txt = str(
            assistant[0].get("text") if isinstance(assistant[0], dict)
            else getattr(assistant[0], "text", "")
        )
        return "I'm on it" in txt or "काम कर रहा" in txt

    if any(s.get("assert_side_effects") or s.get("assert_grounded_count") for s in steps) or any(
        _d1_only(r) for r in results
    ):
        _late = _sweep_late_replies(dsn, tenant_id, results, verbose=verbose)
        # VT-633 #52 — content asserts get the same settle treatment as DB asserts: a step whose
        # assert_contains / assert_not_d1 judged only the in-window D1 ack is RE-EVALUATED against
        # its transcript plus the swept out-of-band replies (the conversation the owner actually
        # had). Only failing steps that declared content asserts are re-checked; DB-assert
        # failures (already settle-polled) are preserved as-is.
        if True:  # VT-633 #54 — re-eval runs even with no swept rows: an out-of-band reply that
            # landed during a LATER step's capture window is in THAT step's transcript, not in
            # _late; the failing step must be re-checked against the FULL conversation either way.
            _all_turns = [t for _res in results for t in _res.transcript] + list(_late)
            for _i, (_step, _r) in enumerate(zip(steps, results)):
                # VT-633 #54 — also re-evaluate steps that failed assert_no_silent: an approval
                # turn is legitimately reply-less IN-WINDOW (try_resume consumes it; the real
                # confirmation is the loop's outcome report, which the sweep just recovered).
                if _r.label != "FAIL" or not (
                    _step.get("assert_contains") or _step.get("assert_not_d1")
                    or any(f.startswith("assert_no_silent") for f in _r.reasons)
                ):
                    continue
                _content_failures = evaluate_assertions(
                    _all_turns,
                    run_status=_r.run_status,
                    assert_no_silent=bool(_step.get("assert_no_silent", True)),
                    assert_contains=_step.get("assert_contains"),
                    assert_not_contains=_step.get("assert_not_contains"),
                    assert_not_d1=bool(_step.get("assert_not_d1", False)),
                    assert_run_reason=_step.get("assert_run_reason"),
                    assert_run_reason_not=_step.get("assert_run_reason_not"),
                )
                _db_failures = [
                    f for f in _r.reasons
                    if f.startswith("assert_side_effects") or f.startswith("assert_route")
                    or f.startswith("assert_grounded") or f.startswith("DB-state assert")
                ]
                _new = _content_failures + _db_failures
                if not _new:
                    if verbose:
                        print(f"  [late-reply re-eval] step {_i + 1}: content asserts now PASS "
                              "against the settled transcript")
                    _ok, _xf, _label = classify_step([], expected_fail=bool(
                        _step.get("expected_fail", scenario_xfail)))
                    results[_i] = StepResult(
                        ok=_ok, xfail=_xf, label=_label, reasons=[],
                        transcript=_r.transcript, run_status=_r.run_status,
                        ingress_reason=_r.ingress_reason, run_id=_r.run_id,
                    )

    # VT-611 Package H1 — the safety-net check, ON BY DEFAULT for every scenario (not a per-step
    # opt-in): no customer send may have gone out without a matching approved decision, ANYWHERE in
    # this scenario's run. Evaluated once, tenant-wide, after all steps (an unapproved send could be
    # a delayed side effect of an earlier step, not necessarily the step that triggered it).
    with _connect(dsn) as conn:
        unapproved = assert_no_unapproved_effect(conn, tenant_id)
    if unapproved:
        if verbose:
            print("\n  [scenario-level] FAIL — assert_no_unapproved_effect")
            for f in unapproved:
                print(f"      - {f}")
        if results:
            results[-1] = StepResult(
                ok=False, xfail=False, label="FAIL", reasons=results[-1].reasons + unapproved,
                transcript=results[-1].transcript, run_status=results[-1].run_status,
                ingress_reason=results[-1].ingress_reason, run_id=results[-1].run_id,
            )
    return results


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

    results = run_scenario_steps(
        dsn, base, secret, args.tenant_id, steps, timeout=args.timeout, scenario_xfail=scenario_xfail,
    )

    passed = sum(1 for r in results if r.label == "PASS")
    xfailed = sum(1 for r in results if r.label == "XFAIL")
    xpassed = sum(1 for r in results if r.label == "XPASS")
    failed = sum(1 for r in results if r.label == "FAIL")
    timed_out = sum(1 for r in results if r.label == "TIMEOUT")
    print(f"\n=== summary: {passed} PASS, {xfailed} XFAIL (known gap), {xpassed} XPASS, {failed} FAIL, "
          f"{timed_out} TIMEOUT ===")
    if xpassed:
        print("    note: XPASS = a marked-gap step unexpectedly passed — the gap may have closed; re-check the mark.")
    if timed_out:
        print("    note: TIMEOUT = the run hadn't completed within --timeout — NOT a silent drop; re-run "
              "with a larger --timeout before treating this as a regression.")

    if args.json_report:
        summary = {
            "passed": passed, "xfailed": xfailed, "xpassed": xpassed,
            "failed": failed, "timed_out": timed_out,
        }
        entry = _build_json_report(scenario, args.file, args.tenant_id, steps, results, summary)
        _append_json_report(args.json_report, entry)
        print(f"    json-report: appended to {args.json_report}")

    return 0 if failed == 0 and timed_out == 0 else 1


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
        # VT-633 #53 — CANCEL the tenant's durable manager_task workflows BEFORE deleting the
        # tenant. Teardown-mid-workflow left orphans that DBOS recovery re-ran after every
        # redeploy, crashing on tenant FKs (dbos.workflow_status: ForeignKeyViolation "Key is
        # not present in table tenants" on incidents/pipeline_runs) and churning dispatch
        # capacity during measurement packs. Direct system-DB status flip (the harness runs
        # outside the DBOS app process, so the client cancel API isn't available here); DBOS
        # recovery skips CANCELLED workflows. Fail-soft: an unreachable system DB must never
        # block the teardown itself.
        try:
            import re as _re

            _sysdsn = _re.sub(r"/([^/?]+)(\?|$)", r"/postgres_dbos_sys\2", dsn, count=1)
            import psycopg as _psycopg

            with _psycopg.connect(_sysdsn, autocommit=True, connect_timeout=10) as _sc:
                _n = _sc.execute(
                    "UPDATE dbos.workflow_status SET status = 'CANCELLED' "
                    "WHERE workflow_uuid LIKE %s AND status IN ('PENDING', 'ENQUEUED')",
                    (f"manager_task:{args.tenant_id}:%",),
                ).rowcount
            if _n:
                print(f"[teardown] cancelled {_n} in-flight manager_task workflow(s)")
        except Exception as _exc:  # noqa: BLE001 — teardown must proceed regardless
            print(f"[teardown] workflow-cancel skipped ({type(_exc).__name__})")

        # VT-620: FK-safe delete via the shared helper (also used by the test-tenant reaper).
        # Root cause of the old leak: pipeline_steps (non-cascade FKs to BOTH pipeline_runs AND
        # tenants) was never deleted before pipeline_runs, so the fixed 2-pass sweep left residual
        # rows AND the try/except swallowed the failure — the tenant delete silently no-op'd. The
        # shared helper deletes pipeline_steps first, FK-orders the rest, and REPORTS the residual
        # (does NOT swallow it). conn is autocommit, so a per-table FK failure can't poison the txn.
        from orchestrator.test_tenant_reaper import fk_safe_delete_tenant

        blocked = fk_safe_delete_tenant(conn, args.tenant_id)
        left = conn.execute(
            "SELECT count(*) FROM tenants WHERE id = %s", (args.tenant_id,)
        ).fetchone()
        remaining = int(left[0] if not isinstance(left, dict) else left["count"])
    if blocked:
        print(f"[teardown] tenant {args.tenant_id} ({name}): NOT fully deleted — still blocked by "
              f"{blocked}; tenant rows left = {remaining}")
    else:
        print(f"[teardown] tenant {args.tenant_id} ({name}): deleted; tenant rows left = {remaining}")
    return 0 if (remaining == 0 and not blocked) else 1


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
    s.add_argument(
        "--journey", action="store_true",
        help="replicate REAL signup's post-create state (VT-582 calibration fix): seed a "
             "business_profile_draft + start an ACTIVE onboarding_journey with a small deterministic "
             "2-entry queue (confirm business_type + gap 'about') — NO LLM calls. Mutually exclusive "
             "with --onboarded (active vs complete). See --draft-about/--draft-city/--draft-type.",
    )
    s.add_argument(
        "--draft-about", default="We serve traditional Indian sweets and snacks, made fresh daily.",
        help="business_profile_draft 'about' attribute (--journey only)",
    )
    s.add_argument(
        "--draft-city", default="Chennai", help="business_profile_draft 'city' attribute (--journey only)"
    )
    s.add_argument(
        "--draft-type", default="sweets",
        help="business_profile_draft 'business_type' attribute — a config/business_types.yaml key "
             "(--journey only; default 'sweets')",
    )
    s.add_argument(
        "--flow", default=None,
        help="arm answers['__flow__'] on a COMPLETE journey (requires --onboarded) for flow-beat "
             "scenarios: 'profile_previewed' | 'ready_asked' | 'deferred' | 'integration:<name>' "
             "(e.g. 'integration:shopify') | 'plan_kicked'",
    )
    s.add_argument(
        "--seed-lapsed-customers", type=int, default=None, metavar="N",
        help="requires --onboarded. AFTER the onboarded seed, insert N bogus customers (majority "
             "old+high-spend / a few recent+low-spend) + matching sale-ledger rows + a marketing-"
             "cleared consent row each + a connected data-source connector + verification/ownership "
             "— the full sales_recovery activation-gate substrate — so a win-back ask can DELEGATE "
             "to the Sales-Recovery specialist and ground a real plan. See --consent-version.",
    )
    s.add_argument(
        "--consent-version", default="dev-test-v0",
        help="record_of_consent.consent_text_version for --seed-lapsed-customers rows. MUST match "
             "a member of dev's Railway MARKETING_CONSENT_VERSIONS allowlist (VT-396 dev-test hook) "
             "or detection structurally returns zero candidates regardless of this seed (default "
             "'dev-test-v0', the VT-396 plan's documented dev-test convention — confirm it against "
             "the actual dev env var before relying on a non-empty cohort).",
    )
    s.add_argument(
        "--ingress-url", default=None,
        help="VT-598 addendum: deployed dev orchestrator base URL (or set TEAM_ORCHESTRATOR_URL). "
             "When --seed-lapsed-customers is ALSO given and DEV_TEST_INGRESS_SECRET is set, "
             "consent is seeded via the deployed service's dev-test consent-seed endpoint "
             "(server-side salt — the correct path); omitted, seeding falls back to a LOCAL "
             "record_consent call that will NOT match on deployed dev (see "
             "_seed_lapsed_customers's docstring).",
    )
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
    sc.add_argument(
        "--json-report", default=None, metavar="PATH",
        help="VT-598: append this scenario's machine-readable transcript bundle (FULL multi-line "
             "replies, never truncated) to PATH — creates it if absent, accumulates across "
             "scenarios if PATH is reused across multiple `script` invocations (a pack run). Feeds "
             "canaries/transcript_judge.py.",
    )
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
