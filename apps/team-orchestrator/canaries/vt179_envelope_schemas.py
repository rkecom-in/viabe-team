#!/usr/bin/env python3
"""VT-179 typed-envelope-schemas canary (Rule #15, DR-15).

Subshell-source nothing — pure-Python canary, NO secrets needed:

    cd apps/team-orchestrator
    time ./.venv/bin/python canaries/vt179_envelope_schemas.py 2>&1 | tee /tmp/vt179-canary-evidence.log | tail -150

**NO secrets sourced.** Pillar 1 / defense-in-depth: this canary touches NO
external API, NO database, NO file I/O beyond Python source-walk for
drift detection. ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 10s. Cost budget: 0 paise.

8 assertions across 4 groups:
- Group A (3): registry completeness — all enumerated step_kinds present,
  all values are StepEnvelope subclasses, no duplicates.
- Group B (3): round-trip serialization — every envelope JSON round-trips
  byte-identical, parses under stdlib json, uses canonical column names.
- Group C (1): drift detection — envelope_for(unknown) raises
  EnvelopeNotRegistered.
- Group D (1): zero-LLM invariant.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
SAMPLE: dict[str, Any] = {}


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight():
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary's loader "
            "must NOT source anthropic.env (Pillar 1 structural enforcement).",
            file=sys.stderr,
        )
        sys.exit(2)
    print("PREFLIGHT OK — ANTHROPIC_API_KEY: <absent — defense-in-depth>")


# Expected step_kind set per VT-179 brief + CL-266 ground-truth audit.
# 15 brief-enumerated + 2 audit-surfaced = 17 total.
EXPECTED_STEP_KINDS = {
    "webhook_received",
    "webhook_classified",
    "state_transition",
    "agent_invocation",
    "agent_reasoning_step",
    "mcp_tool_call",
    "self_evaluate_gate",
    "campaign_plan_emitted",
    "message_dispatch",
    "attribution_match",
    "day39_evaluator",
    "refund_decision",
    "opt_out_processed",
    "dsr_processed",
    "error",
    # Audit-surfaced (CL-266 ground-truth reconciliation):
    "context_truncation",
    "tenant_isolation_breach",
}


# Canonical column-name set every envelope must serialize using.
CANONICAL_FIELD_NAMES = {
    "run_id", "tenant_id", "step_seq", "step_name", "parent_step_id",
    "status", "decision_rationale", "model_used", "tokens_input",
    "tokens_output", "tool_calls", "started_at", "ended_at", "error",
    "input_envelope", "output_envelope",
}

# Legacy names that MUST NOT appear in any envelope serialization.
FORBIDDEN_FIELD_NAMES = {
    "step_index", "rationale", "error_envelope", "stepSeq", "stepName",
    "decisionRationale", "modelUsed", "tokensInput", "tokensOutput",
}


def _build_minimal_instance(env_cls) -> Any:
    """Construct an envelope instance with the minimum fields, using empty
    sub-models for ``input_envelope`` / ``output_envelope``."""
    input_cls = env_cls.model_fields["input_envelope"].annotation
    input_instance = _build_minimal_submodel(input_cls)
    kwargs: dict[str, Any] = {
        "run_id": uuid4(),
        "tenant_id": uuid4(),
        "step_seq": 1,
        "started_at": datetime.now(timezone.utc),
        "input_envelope": input_instance,
    }
    output_field = env_cls.model_fields.get("output_envelope")
    if output_field is not None:
        output_annotation = output_field.annotation
        output_instance = _build_minimal_submodel(output_annotation)
        if output_instance is not None:
            kwargs["output_envelope"] = output_instance
    return env_cls(**kwargs)


def _build_minimal_submodel(annotation) -> Any:
    """Return an instance with empty/default fields for a Pydantic sub-model.

    Returns None for `None` or `BaseModel | None`. Handles `Literal[...]` by
    picking the first value, `str` defaults to "x", `int` defaults to 0,
    etc.
    """
    import typing

    from pydantic import BaseModel

    if annotation is None or annotation is type(None):
        return None
    origin = typing.get_origin(annotation)
    if origin is typing.Union or (origin is not None and type(None) in typing.get_args(annotation)):
        # Union — pick the first non-None arg
        for arg in typing.get_args(annotation):
            if arg is type(None):
                continue
            return _build_minimal_submodel(arg)
        return None

    if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        return None

    kwargs: dict[str, Any] = {}
    for name, finfo in annotation.model_fields.items():
        ann = finfo.annotation
        kwargs[name] = _scalar_default(ann)
    return annotation(**kwargs)


def _scalar_default(ann) -> Any:
    """Pick a minimal scalar default for an annotation."""
    import typing

    origin = typing.get_origin(ann)
    if origin is typing.Literal:
        return typing.get_args(ann)[0]
    if origin is typing.Union or (origin is not None and type(None) in typing.get_args(ann)):
        for arg in typing.get_args(ann):
            if arg is type(None):
                continue
            return _scalar_default(arg)
        return None
    if ann is str:
        return "x"
    if ann is int:
        return 0
    if ann is bool:
        return False
    if ann is float:
        return 0.0
    if ann is UUID:
        return uuid4()
    if origin is list:
        return []
    if origin is dict or ann is dict:
        return {}
    return None


def run_canary() -> int:
    _preflight()

    from orchestrator.observability.envelopes import (
        STEP_KIND_REGISTRY,
        EnvelopeNotRegistered,
        StepEnvelope,
        envelope_for,
        validate_registry_completeness,
    )

    # -----------------------------------------------------------------
    # Group A — registry completeness (3 assertions)
    # -----------------------------------------------------------------

    registry_keys = set(STEP_KIND_REGISTRY.keys())
    SAMPLE["registry_keys"] = sorted(registry_keys)
    SAMPLE["registry_size"] = len(registry_keys)

    # Assertion 1 — every expected kind present.
    missing = EXPECTED_STEP_KINDS - registry_keys
    extras = registry_keys - EXPECTED_STEP_KINDS
    pass_1 = not missing and not extras
    assertion(
        1,
        "STEP_KIND_REGISTRY matches EXPECTED set (15 brief + 2 audit-surfaced = 17)",
        pass_1,
        observed={"missing": sorted(missing), "unexpected_extras": sorted(extras)},
        expected={"missing": [], "unexpected_extras": []},
    )

    # Assertion 2 — every registry value is a StepEnvelope subclass.
    bad_types: list[str] = []
    for kind, cls in STEP_KIND_REGISTRY.items():
        if not (isinstance(cls, type) and issubclass(cls, StepEnvelope)):
            bad_types.append(f"{kind}={cls!r}")
    pass_2 = not bad_types
    assertion(
        2,
        "Every STEP_KIND_REGISTRY value is a type[StepEnvelope] subclass",
        pass_2,
        observed={"bad_types": bad_types, "total_values": len(STEP_KIND_REGISTRY)},
        expected={"bad_types": []},
    )

    # Assertion 3 — no duplicate VALUES (same class registered under > 1 key).
    seen: dict[type, list[str]] = {}
    for kind, cls in STEP_KIND_REGISTRY.items():
        seen.setdefault(cls, []).append(kind)
    duplicates = {cls.__name__: keys for cls, keys in seen.items() if len(keys) > 1}
    pass_3 = not duplicates
    assertion(
        3,
        "No registry value is registered under multiple keys (no class collisions)",
        pass_3,
        observed={"duplicates": duplicates},
        expected={"duplicates": {}},
    )

    # -----------------------------------------------------------------
    # Group B — round-trip serialization (3 assertions)
    # -----------------------------------------------------------------

    round_trip_failures: list[str] = []
    parse_failures: list[str] = []
    forbidden_hits: dict[str, list[str]] = {}
    canonical_field_examples: dict[str, list[str]] = {}

    for kind, env_cls in STEP_KIND_REGISTRY.items():
        try:
            instance = _build_minimal_instance(env_cls)
        except Exception as exc:
            round_trip_failures.append(f"{kind}: construct failed — {exc!r}")
            parse_failures.append(f"{kind}: construct failed — {exc!r}")
            continue
        try:
            payload = instance.model_dump_json()
            re_instance = env_cls.model_validate_json(payload)
            re_payload = re_instance.model_dump_json()
            if payload != re_payload:
                round_trip_failures.append(f"{kind}: bytes differ")
        except Exception as exc:
            round_trip_failures.append(f"{kind}: round-trip raise — {exc!r}")

        try:
            parsed = json.loads(payload)
        except Exception as exc:
            parse_failures.append(f"{kind}: json.loads raise — {exc!r}")
            continue

        # Forbidden legacy/camelCase names anywhere in the JSON tree.
        hits = sorted(_walk_keys(parsed) & FORBIDDEN_FIELD_NAMES)
        if hits:
            forbidden_hits[kind] = hits

        top_level = sorted(parsed.keys())
        canonical_field_examples[kind] = top_level

    SAMPLE["sample_envelope_keys"] = canonical_field_examples
    pass_4 = not round_trip_failures
    pass_5 = not parse_failures
    pass_6 = not forbidden_hits

    assertion(
        4,
        "Every envelope: instance → model_dump_json → model_validate_json → byte-identical",
        pass_4,
        observed={"failures": round_trip_failures, "total_envelopes": len(STEP_KIND_REGISTRY)},
        expected={"failures": []},
    )
    assertion(
        5,
        "Every envelope JSON parses cleanly under stdlib json.loads()",
        pass_5,
        observed={"failures": parse_failures, "total_envelopes": len(STEP_KIND_REGISTRY)},
        expected={"failures": []},
    )
    assertion(
        6,
        "No envelope uses legacy/camelCase field names (canonical-column-name discipline per CL-417)",
        pass_6,
        observed={"forbidden_hits": forbidden_hits},
        expected={"forbidden_hits": {}},
    )

    # -----------------------------------------------------------------
    # Group C — drift detection (1 assertion)
    # -----------------------------------------------------------------

    drift_raised = False
    try:
        envelope_for("definitely_not_a_real_step_kind_x9z")
    except EnvelopeNotRegistered:
        drift_raised = True

    # And: validate_registry_completeness must not raise on the current source.
    validate_raised: str | None = None
    try:
        validate_registry_completeness()
    except Exception as exc:
        validate_raised = repr(exc)

    pass_7 = drift_raised and validate_raised is None
    assertion(
        7,
        "envelope_for(unregistered) raises EnvelopeNotRegistered; current source is registry-complete",
        pass_7,
        observed={
            "envelope_for_unknown_raised_EnvelopeNotRegistered": drift_raised,
            "validate_registry_completeness_raised": validate_raised,
        },
        expected={
            "envelope_for_unknown_raised_EnvelopeNotRegistered": True,
            "validate_registry_completeness_raised": None,
        },
    )

    # -----------------------------------------------------------------
    # Group D — zero LLM (1 assertion)
    # -----------------------------------------------------------------

    pass_8 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        8,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_8,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise()


def _walk_keys(obj: Any) -> set[str]:
    """Recursively collect every dict key in a JSON-loaded value."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                found.add(k)
            found |= _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _walk_keys(item)
    return found


def _finalise() -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (pure-Python canary; no DB; no LLM) ===")

    print("\n=== SAMPLE (registry + per-envelope serialized field names) ===")
    print(json.dumps(SAMPLE, indent=2, default=str))

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 8 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
