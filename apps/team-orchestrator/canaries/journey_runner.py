"""Phase-1.1 journey runner (`.viabe/journey-sim-spec.md`) — chains an ordered list of PHASES
against ONE synthetic tenant, so a journey reads as a single continuous conversation arc (onboard →
connect → ask → plan → approve → send, etc.) instead of the one-scenario-one-tenant shape
``run_full_pack.py`` / ``run_critical_x3.py`` use.

Builds entirely on ``convo_harness.py``'s existing primitives (setup/teardown, ``run_scenario_steps``,
the Package H1 DB-state asserts) — this file adds no new DB/HTTP surface of its own, only the
phase-chaining + owner-persona layer on top. Mirrors ``run_full_pack.py``'s in-process reuse pattern
(``ch.build_parser()`` + ``ch.cmd_setup``/``ch.cmd_teardown``, no subprocess).

JOURNEY FILE SCHEMA (``canaries/journeys/*.json``):
    {
      "name": "...",
      "notes": "...",                       # optional, free text
      "persona": {                          # optional; required for any persona_turn step
        "business": "...", "temperament": "...", "backstory": "...",
        "style": "hinglish" | "en" | "devanagari"   # per-step override available too
      },
      "setup_args": ["--onboarded", "--seed-lapsed-customers", "10"],  # ONE tenant, set up once
      "phases": [
        {
          "name": "...",                    # optional, for reporting
          "steps": [ {...} ],                # EXACTLY ONE of "steps" / "scenario_ref"
          "scenario_ref": "some_scenario.json",   # a canaries/scenarios/*.json FILENAME — its OWN
                                             # steps run against the journey's tenant; its OWN
                                             # setup_args are intentionally never read (the journey
                                             # already set the tenant up)
          "expected_fail": false,           # optional default for this phase's steps (scenario_ref
                                             # phases instead default from the referenced file's own
                                             # "expected_fail")
          "wait_s": 5,                      # optional: sleep AFTER this phase, before the next
          "db_asserts": {                   # optional: TENANT-WIDE DB-state proof after this phase
            "assert_route": {"expect_sr_delegation": true},
            "assert_side_effects": {"expect_sent_count_at_least": 1},
            "assert_grounded_count": {"expected_count": 8}
          }
        }
      ]
    }

Each step is the SAME schema ``convo_harness.py``'s scenarios use (``message`` + any ``assert_*``
key) — OR an owner-persona step:
    {"persona_turn": {"goal": "...", "style": "hinglish"}, "fallback_message": "...", "assert_*": ...}

``persona_turn`` — ONE live Anthropic call (model ``claude-sonnet-5``, lazy-imported, reads
``ANTHROPIC_API_KEY`` from env) voices the owner's next message from the journey's ``persona`` block
+ the goal + the REAL conversation-so-far (this journey's actual turns, not a script) — reactive,
not scripted. The persona is BLIND: it is never given any ``assert_*`` field, so it can't be steered
toward (or away from) whatever the scenario is checking. No API key -> the step's REQUIRED
``fallback_message`` is used verbatim (deterministic, no network) — this is a MISSING-key fallback
only; a key that IS present but whose call fails raises (fail-not-skip, same discipline as
transcript_judge.py's judge_batch).

Every resolved step (persona-generated or literal) is driven ONE AT A TIME through
``ch.run_scenario_steps`` (a length-1 step list per call) rather than batching a whole phase into one
call — so a persona_turn later in the SAME phase reacts to the REAL reply of an earlier step in that
phase, and the VT-633 late-reply-sweep / VT-611 assert_no_unapproved_effect safety net apply with
STEP-level precision (tighter than the batched-phase scope, not looser: a failure is attributable to
the exact step that caused it).

Emits ONE json-report bundle entry per journey run, in the EXACT shape
``convo_harness.py``'s ``_build_json_report`` produces (name/setup_args/notes/steps/summary) — the
whole journey's flattened step list, so ``transcript_judge.py`` / ``tier_rescore.py`` consume it
unchanged, scoring the journey as one continuous transcript. An extra ``"phases"`` key (name/source/
step-count/db_assert_failures per phase) is appended for traceability — both consumers only read the
keys they already know, so this is additive, never a shape break.

Usage (on deployed dev):

    railway run --service vt-orchestrator-service --environment development -- \\
        uv run --directory apps/team-orchestrator python canaries/journey_runner.py \\
        canaries/journeys/j01_shopify_winback.json \\
        [--ingress-url URL] [--timeout S] [--keep-tenants] [--json-report PATH] \\
        [--scenarios-dir canaries/scenarios]

NOTE: the 10-journey × 3-run measurement CAMPAIGN this runner exists to serve is HELD pending
Fazal's greenlight (see journey-sim-spec.md's status line) — this file is the harness capability
only, not an authorization to run it against deployed dev.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_CANARIES = Path(__file__).resolve().parent
sys.path.insert(0, str(_CANARIES))  # allow `import convo_harness` regardless of caller's cwd

import convo_harness as ch  # noqa: E402 — after the sys.path insert

# --- persona constants ---------------------------------------------------------------------------

# claude-sonnet-5 (per the journey-sim-spec brief) — cheaper + plenty capable for voicing a short
# WhatsApp message; the JUDGE (transcript_judge.py/tier_rescore.py) is the model that needs to be the
# most discerning, not the persona doing the asking.
PERSONA_MODEL = "claude-sonnet-5"
_PERSONA_MAX_TOKENS = 300

_STYLE_INSTRUCTIONS: dict[str, str] = {
    "en": "Write in plain, casual English.",
    "hinglish": "Write in casual Hinglish (Roman-script Hindi-English mix) — the way a busy Indian "
                "small-business owner texts on WhatsApp.",
    "devanagari": "Write in Hindi using Devanagari script.",
}
_DEFAULT_STYLE = "en"


# --- pure functions (unit-tested; no DB/network) -------------------------------------------------


def load_journey(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _validate_phase_shape(phase: dict[str, Any]) -> None:
    """Fail loudly on a structurally broken phase — EXACTLY ONE of ``steps`` / ``scenario_ref``,
    never both, never neither (a phase with neither would silently run zero steps)."""
    has_steps = "steps" in phase
    has_ref = "scenario_ref" in phase
    if has_steps == has_ref:
        raise ValueError(
            f"phase {phase.get('name', '<unnamed>')!r} must set EXACTLY ONE of 'steps' or "
            f"'scenario_ref' (got steps={has_steps} scenario_ref={has_ref})"
        )


def validate_journey(journey: dict[str, Any]) -> None:
    """Cheap, no-file-IO structural check — a journey with zero phases or a malformed phase is a
    authoring bug, reported immediately rather than discovered mid-run after a tenant is already
    provisioned."""
    phases = journey.get("phases")
    if not phases:
        raise ValueError("journey has no 'phases' (or an empty list) — nothing to run")
    for phase in phases:
        _validate_phase_shape(phase)


def resolve_phase_source(
    phase: dict[str, Any], scenarios_dir: Path,
) -> tuple[str, list[dict[str, Any]], bool]:
    """(source_label, steps, default_xfail) for one phase. ``scenario_ref`` loads the referenced
    file's OWN steps + its OWN ``expected_fail`` as the default (mirrors ``run_full_pack.py``
    threading a scenario's ``expected_fail`` through as ``scenario_xfail``) — its ``setup_args`` are
    intentionally NEVER read here; the journey's tenant is already set up before any phase runs."""
    _validate_phase_shape(phase)
    if "scenario_ref" in phase:
        ref = str(phase["scenario_ref"])
        scenario = json.loads((scenarios_dir / ref).read_text(encoding="utf-8"))
        return (
            f"scenario_ref:{ref}", list(scenario.get("steps", [])),
            bool(scenario.get("expected_fail", False)),
        )
    return (
        str(phase.get("name", "<inline>")), list(phase["steps"]),
        bool(phase.get("expected_fail", False)),
    )


def style_instruction(style: str) -> str:
    return _STYLE_INSTRUCTIONS.get(style, _STYLE_INSTRUCTIONS[_DEFAULT_STYLE])


def build_persona_system_prompt(persona: dict[str, Any], style: str) -> str:
    business = persona.get("business", "a small Indian business")
    temperament = persona.get("temperament", "")
    backstory = persona.get("backstory", "")
    lines = [
        f"You are the OWNER of {business}, texting your AI business assistant on WhatsApp.",
        style_instruction(style),
    ]
    if temperament:
        lines.append(f"Temperament: {temperament}.")
    if backstory:
        lines.append(backstory)
    lines.append(
        "Stay fully in character as the owner — you have not seen how this conversation plays out "
        "in advance; react naturally to whatever the assistant just said."
    )
    return "\n".join(lines)


def render_conversation_for_persona(turns: list[Any]) -> str:
    """Plain-text transcript for the persona's user turn — accepts ``ch.Turn`` instances OR plain
    dicts (tests use both). The internal ``[internal route: ...]`` marker (role='system') is never
    shown to the persona — it's an implementation-detail signal for the DB-state asserts, not
    something a real owner would ever see."""
    lines = []
    for t in turns:
        role = t.get("role") if isinstance(t, dict) else getattr(t, "role", None)
        if role == "system":
            continue
        text = t.get("text") if isinstance(t, dict) else getattr(t, "text", "")
        speaker = "You (owner)" if role == "owner" else "Assistant"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines) if lines else "(conversation just starting)"


def build_persona_user_content(goal: str, conversation_so_far: str) -> str:
    return (
        f"CONVERSATION SO FAR:\n{conversation_so_far}\n\n"
        f"Your goal for this next message: {goal}\n\n"
        "Reply with ONLY the next WhatsApp message you would send as the owner — no preamble, no "
        "quotation marks, no explanation of your reasoning."
    )


def resolve_step_message(
    step: dict[str, Any], persona: dict[str, Any], conversation_so_far: str, *,
    api_key_present: bool, call_persona: Callable[[str, str], str] | None = None,
) -> str:
    """The literal message text to send for one journey step: ``step["message"]`` verbatim, OR — for
    a ``persona_turn`` step — a live LLM-voiced owner message when ``api_key_present``, else the
    step's REQUIRED ``fallback_message`` (deterministic, no network). The persona is BLIND: nothing
    here ever reads an ``assert_*`` key off ``step`` when building the persona's prompt — only
    ``goal``/``style`` + the persona block + the real conversation so far.

    Pure: the actual Anthropic call is injected as ``call_persona`` (system, user_content) -> text —
    never constructed in this function. Real network wiring lives in ``generate_persona_message`` /
    ``_persona_client`` (impure, lazy-imported), wired up by the caller in ``run_journey``."""
    turn = step.get("persona_turn")
    if turn is None:
        message = step.get("message")
        if not message:
            raise ValueError(f"step has neither 'message' nor 'persona_turn': {step!r}")
        return str(message)

    fallback = step.get("fallback_message")
    if not fallback:
        raise ValueError(
            "a 'persona_turn' step MUST set 'fallback_message' (the deterministic, no-API-key "
            f"stand-in) — got {step!r}"
        )
    if not api_key_present:
        return str(fallback)

    goal = turn.get("goal")
    if not goal:
        raise ValueError(f"'persona_turn' requires a 'goal': {step!r}")
    style = turn.get("style") or persona.get("style", _DEFAULT_STYLE)
    system = build_persona_system_prompt(persona, style)
    user_content = build_persona_user_content(goal, conversation_so_far)
    if call_persona is None:
        raise ValueError(
            "api_key_present=True but no call_persona was injected — the caller must wire one "
            "(see run_journey)"
        )
    text = call_persona(system, user_content).strip()
    if not text:
        raise ValueError("persona call returned empty text")
    return text


def evaluate_phase_db_asserts(dsn: str, tenant_id: str, db_asserts: dict[str, Any] | None) -> list[str]:
    """Phase-level DB-state proof (Package H1's ``assert_route``/``assert_side_effects``/
    ``assert_grounded_count``, reused as-is) — ALWAYS tenant-wide (``run_id=None``): a phase may
    drive zero, one, or many turns, so there is no single run_id to scope to (unlike a per-step
    assert, which scopes to that step's own turn unless it opts into ``tenant_wide``). Absent/empty
    ``db_asserts`` -> zero DB round-trips."""
    if not db_asserts:
        return []
    failures: list[str] = []
    with ch._connect(dsn) as conn:
        if "assert_route" in db_asserts:
            failures += ch.assert_route(conn, tenant_id, None, **db_asserts["assert_route"])
        if "assert_side_effects" in db_asserts:
            failures += ch.assert_side_effects(conn, tenant_id, None, **db_asserts["assert_side_effects"])
        if "assert_grounded_count" in db_asserts:
            failures += ch.assert_grounded_count(
                conn, tenant_id, None, **db_asserts["assert_grounded_count"]
            )
    return failures


def _fold_failures_into_last(results: list[ch.StepResult], failures: list[str]) -> None:
    """Attach phase-level DB-assert failures onto the LAST driven step's result — mirrors
    ``run_scenario_steps``'s own treatment of its scenario-level ``assert_no_unapproved_effect``
    check. A no-op when there's nothing to fold onto (an empty phase) or nothing to fold."""
    if not failures or not results:
        return
    last = results[-1]
    results[-1] = ch.StepResult(
        ok=False, xfail=False, label="FAIL", reasons=list(last.reasons) + failures,
        transcript=last.transcript, run_status=last.run_status, ingress_reason=last.ingress_reason,
        run_id=last.run_id,
    )


# --- Anthropic call (lazy import — mirrors convo_harness.py/transcript_judge.py's dep-less-at-import
# -time posture) ------------------------------------------------------------------------------------


def _persona_client() -> Any:
    from anthropic import Anthropic

    return Anthropic()  # reads ANTHROPIC_API_KEY from env — never hardcode a key in this file


def generate_persona_message(
    system: str, user_content: str, *, client: Any, model: str = PERSONA_MODEL,
) -> str:
    """One live Anthropic call voicing the owner persona's next message. Fail-not-skip (Rule #15):
    raises on an empty/garbled response rather than silently falling back — the deterministic
    ``fallback_message`` path in ``resolve_step_message`` is for a MISSING key only."""
    response = client.messages.create(
        model=model, max_tokens=_PERSONA_MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(getattr(b, "text", "") or "" for b in getattr(response, "content", []) or [])
    text = text.strip()
    if not text:
        raise ValueError("persona call returned empty text")
    return text


# --- orchestration (real DB/HTTP; not unit-tested directly — the logic above is) ------------------


@dataclass
class PhaseReport:
    name: str
    source: str
    n_steps: int
    db_assert_failures: list[str]


@dataclass
class JourneyRunResult:
    tenant_id: str
    phases: list[PhaseReport]
    steps: list[dict[str, Any]]
    results: list[ch.StepResult]


def _setup_tenant(setup_args: list[Any], *, ingress_url: str | None, run_label: str) -> str:
    """Provision the journey's ONE tenant via the REAL ``convo_harness setup`` CLI parser (in-process,
    no subprocess) — same reuse pattern as ``run_full_pack.py``'s ``_setup_tenant``."""
    parser = ch.build_parser()
    argv = [
        "setup", *[str(a) for a in setup_args],
        "--name", f"convo-harness-journey-{run_label}-{uuid.uuid4().hex[:8]}",
    ]
    if ingress_url:
        argv += ["--ingress-url", ingress_url]
    ns = parser.parse_args(argv)
    ch.cmd_setup(ns)
    return str(ns.tenant_id)


def run_journey(
    journey: dict[str, Any], *, scenarios_dir: Path, ingress_url: str | None, timeout: float,
    keep_tenants: bool, verbose: bool = True,
) -> JourneyRunResult:
    """Drive every phase of ``journey`` against ONE fresh harness tenant, in order. Each resolved
    step is driven through ``ch.run_scenario_steps`` one at a time (a length-1 list per call) — see
    the module docstring for why (persona reactivity + step-precise safety-net attribution)."""
    validate_journey(journey)
    dsn = ch._dsn()
    base = ch._ingress_base(ingress_url)
    secret = ch._dev_secret()
    name = str(journey.get("name", "journey"))
    persona = journey.get("persona", {})
    setup_args = journey.get("setup_args", [])

    tenant_id = _setup_tenant(setup_args, ingress_url=ingress_url, run_label=name)
    client: Any = None
    conversation: list[Any] = []
    all_steps: list[dict[str, Any]] = []
    all_results: list[ch.StepResult] = []
    phase_reports: list[PhaseReport] = []
    try:
        for phase in journey["phases"]:
            source_label, raw_steps, default_xfail = resolve_phase_source(phase, scenarios_dir)
            if verbose:
                print(
                    f"\n=== phase: {phase.get('name', source_label)} "
                    f"({source_label}, {len(raw_steps)} step(s)) ==="
                )
            for step in raw_steps:
                api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
                call_persona = None
                if step.get("persona_turn") is not None and api_key_present:
                    if client is None:
                        client = _persona_client()
                    call_persona = functools.partial(generate_persona_message, client=client)
                convo_text = render_conversation_for_persona(conversation)
                message = resolve_step_message(
                    step, persona, convo_text, api_key_present=api_key_present,
                    call_persona=call_persona,
                )
                resolved = {k: v for k, v in step.items() if k not in ("persona_turn", "fallback_message")}
                resolved["message"] = message
                step_xfail = bool(step.get("expected_fail", default_xfail))
                step_results = ch.run_scenario_steps(
                    dsn, base, secret, tenant_id, [resolved], timeout=timeout,
                    scenario_xfail=step_xfail, verbose=verbose,
                )
                r = step_results[0]
                all_steps.append(resolved)
                all_results.append(r)
                conversation.extend(r.transcript)

            phase_failures = evaluate_phase_db_asserts(dsn, tenant_id, phase.get("db_asserts"))
            if phase_failures:
                _fold_failures_into_last(all_results, phase_failures)
                if verbose:
                    print(f"  [phase db_asserts] FAIL — {'; '.join(phase_failures)}")
            phase_reports.append(PhaseReport(
                name=str(phase.get("name", source_label)), source=source_label,
                n_steps=len(raw_steps), db_assert_failures=phase_failures,
            ))

            wait_s = phase.get("wait_s")
            if wait_s:
                if verbose:
                    print(f"  [wait] sleeping {wait_s}s before the next phase")
                time.sleep(float(wait_s))
    finally:
        if not keep_tenants:
            ch.cmd_teardown(argparse.Namespace(tenant_id=tenant_id))
    return JourneyRunResult(tenant_id=tenant_id, phases=phase_reports, steps=all_steps, results=all_results)


# --- CLI --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="journey_runner", description=__doc__)
    p.add_argument("journey", help="path to a journey JSON file (canaries/journeys/*.json)")
    p.add_argument("--ingress-url", default=None, help="deployed dev orchestrator base URL")
    p.add_argument("--timeout", type=float, default=90.0, help="per-turn run-completion timeout (s)")
    p.add_argument("--keep-tenants", action="store_true", help="skip teardown (debug)")
    p.add_argument(
        "--json-report", default=None, metavar="PATH",
        help="bundle path for transcript_judge.py / tier_rescore.py (same shape as "
             "convo_harness.py's --json-report)",
    )
    p.add_argument(
        "--scenarios-dir", default=str(_CANARIES / "scenarios"),
        help="dir a phase's 'scenario_ref' resolves filenames against",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    journey = load_journey(args.journey)
    name = str(journey.get("name", Path(args.journey).stem))

    print(f"=== journey: {name} ===")
    if journey.get("notes"):
        print(f"    note: {journey['notes']}")

    result = run_journey(
        journey, scenarios_dir=Path(args.scenarios_dir), ingress_url=args.ingress_url,
        timeout=args.timeout, keep_tenants=args.keep_tenants,
    )

    passed = sum(1 for r in result.results if r.label == "PASS")
    xfailed = sum(1 for r in result.results if r.label == "XFAIL")
    xpassed = sum(1 for r in result.results if r.label == "XPASS")
    failed = sum(1 for r in result.results if r.label == "FAIL")
    timed_out = sum(1 for r in result.results if r.label == "TIMEOUT")
    print(
        f"\n=== summary: {len(result.phases)} phase(s), {passed} PASS, {xfailed} XFAIL, "
        f"{xpassed} XPASS, {failed} FAIL, {timed_out} TIMEOUT ==="
    )
    for pr in result.phases:
        if pr.db_assert_failures:
            print(f"  [{pr.name}] phase db_asserts FAILED: {'; '.join(pr.db_assert_failures)}")

    if args.json_report:
        summary = {
            "passed": passed, "xfailed": xfailed, "xpassed": xpassed,
            "failed": failed, "timed_out": timed_out,
        }
        entry = ch._build_json_report(
            {"name": name, "setup_args": journey.get("setup_args", []), "notes": journey.get("notes")},
            args.journey, result.tenant_id, result.steps, result.results, summary,
        )
        entry["phases"] = [
            {
                "name": pr.name, "source": pr.source, "n_steps": pr.n_steps,
                "db_assert_failures": pr.db_assert_failures,
            }
            for pr in result.phases
        ]
        ch._append_json_report(args.json_report, entry)
        print(f"    json-report: appended to {args.json_report}")

    return 0 if failed == 0 and timed_out == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
