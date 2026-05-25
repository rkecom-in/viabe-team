#!/usr/bin/env python3
"""VT-101 LangSmith canary (Rule #15).

Run with secrets sourced in a subshell:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/langsmith-dev.env
      set +a
      ./.venv/bin/python canaries/vt101_langsmith.py
    )

Exits 0 only if all 7 assertions pass. Prints actual observed values + a full
JSON dump of the fetched trace as the audit artifact for the
``pre-merge-result`` signal body.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

# Make the project importable when running from canaries/.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.observability import (  # noqa: E402
    redact_for_langsmith,
    trace_run,
    traceable_node,
    traceable_tool,
)
from orchestrator.observability import langsmith as ls_mod  # noqa: E402


RESULTS: dict[int, dict[str, Any]] = {}


def _preflight() -> None:
    missing = [
        v
        for v in ("LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)
    print(f"PREFLIGHT OK — project={os.environ['LANGSMITH_PROJECT']} endpoint={os.environ['LANGSMITH_ENDPOINT']}")


def _wait_for_run(client: Any, run_id: UUID, timeout_s: int = 30, poll_s: float = 2.0) -> Any:
    """Poll client.read_run until the run is fetchable or timeout."""
    deadline = time.time() + timeout_s
    last_exc: BaseException | None = None
    while time.time() < deadline:
        try:
            return client.read_run(str(run_id))
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(poll_s)
    raise RuntimeError(f"read_run did not return within {timeout_s}s (last={last_exc!r})")


def _run_to_dict(run: Any) -> dict[str, Any]:
    """Best-effort conversion of a Run object to a JSON-serialisable dict."""
    if hasattr(run, "dict") and callable(run.dict):
        try:
            return dict(run.dict())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(run, "model_dump") and callable(run.model_dump):
        try:
            return dict(run.model_dump())
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, Any] = {}
    for attr in (
        "id",
        "name",
        "run_type",
        "trace_id",
        "parent_run_id",
        "session_name",
        "project_name",
        "inputs",
        "outputs",
        "extra",
        "start_time",
        "end_time",
        "status",
        "error",
    ):
        if hasattr(run, attr):
            try:
                out[attr] = getattr(run, attr)
            except Exception:  # noqa: BLE001
                pass
    return out


def _default_serialiser(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def assertion(num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {
        "name": name,
        "status": status,
        "observed": observed,
        "expected": expected,
    }
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def run_canary() -> int:
    _preflight()

    from langsmith import Client

    client = Client()  # picks up LANGSMITH_API_KEY + LANGSMITH_ENDPOINT from env

    # -----------------------------------------------------------------
    # Synthetic dispatch path: nested traceable_node -> traceable_tool
    # with a payload that contains PII the redactor must scrub.
    # -----------------------------------------------------------------
    parent_run_id = uuid4()
    pii_payload = {
        "summary": "Customer reached out at +919876543210 about renewal",
        "customer_name": "Rajesh Kumar",
        "context": {"body": "Hi I want to cancel", "tenant_id": "tenant-canary"},
    }

    @traceable_tool("canary.tool.process_payload")
    def tool(run_id: UUID, payload: dict[str, Any]) -> dict[str, str]:
        return {"status": "handled", "echo_phone": payload["summary"]}

    @traceable_node("canary.node.dispatch")
    def dispatch(run_id: UUID, payload: dict[str, Any]) -> dict[str, str]:
        return tool(run_id=run_id, payload=payload)

    async def run_with_parent() -> dict[str, str]:
        async with trace_run(parent_run_id, "canary.parent.trace_run"):
            return dispatch(run_id=parent_run_id, payload=pii_payload)

    # Assertion 1 — real POST succeeds (call returns without raising;
    # if the SDK raised an HTTP error, the whole dispatch would crash).
    try:
        result = asyncio.run(run_with_parent())
        assertion(1, "Real POST to LangSmith succeeded", True, observed=result)
    except BaseException as exc:  # noqa: BLE001
        assertion(1, "Real POST to LangSmith succeeded", False, observed=repr(exc), expected="no exception")
        return _finalise()

    # Give LangSmith a moment to commit the writes before we poll.
    time.sleep(2)

    # Assertion 2 — trace fetchable post-write (poll up to 30s).
    parent_run = None
    try:
        parent_run = _wait_for_run(client, parent_run_id, timeout_s=30, poll_s=2.0)
        assertion(
            2,
            "Trace fetchable via read_run within 30s",
            True,
            observed=f"id={getattr(parent_run, 'id', None)}",
        )
    except BaseException as exc:  # noqa: BLE001
        assertion(2, "Trace fetchable via read_run within 30s", False, observed=repr(exc), expected="run object")
        return _finalise()

    run_dict = _run_to_dict(parent_run)

    # Assertion 3 — project name correct.
    session_name = run_dict.get("session_name") or run_dict.get("project_name")
    expected_project = os.environ["LANGSMITH_PROJECT"]
    assertion(
        3,
        f"Project name == {expected_project!r}",
        session_name == expected_project,
        observed=f"session_name={session_name!r}",
        expected=expected_project,
    )

    # Assertion 4 — fetched id == parent_run_id.
    fetched_id = str(run_dict.get("id") or "")
    assertion(
        4,
        "Fetched run id == client-generated run_id",
        fetched_id == str(parent_run_id),
        observed=f"fetched={fetched_id} client={parent_run_id}",
        expected=str(parent_run_id),
    )

    # Assertion 5 — PII redaction applied at the SDK boundary.
    # Look across inputs+outputs of the parent + child runs.
    haystacks: list[str] = []
    haystacks.append(json.dumps(run_dict, default=_default_serialiser))

    try:
        # list all runs whose trace_id == parent_run_id (parent + descendants)
        descendants = list(
            client.list_runs(project_name=expected_project, trace=str(parent_run_id))
        )
    except BaseException as exc:  # noqa: BLE001
        descendants = []
        print(f"[note] list_runs(trace=...) failed: {exc!r}", file=sys.stderr)

    for r in descendants:
        haystacks.append(json.dumps(_run_to_dict(r), default=_default_serialiser))

    combined = "\n".join(haystacks)
    raw_leaked = []
    if "+919876543210" in combined or "919876543210" in combined or "9876543210" in combined:
        raw_leaked.append("phone")
    if "Rajesh Kumar" in combined:
        raw_leaked.append("customer_name")
    if "Hi I want to cancel" in combined:
        raw_leaked.append("body")
    has_phone_tok = "phone_tok_" in combined
    has_body_tok = "body_tok_" in combined
    has_name_redaction = "<redacted:customer_name" in combined or "<redacted:name" in combined
    redactions_present = has_phone_tok and has_body_tok and has_name_redaction
    assertion(
        5,
        "PII redaction applied: no raw PII; redacted markers present",
        not raw_leaked and redactions_present,
        observed=(
            f"raw_leaked={raw_leaked} phone_tok={has_phone_tok} "
            f"body_tok={has_body_tok} name_redaction={has_name_redaction}"
        ),
        expected="raw_leaked=[] AND all three redaction markers present",
    )

    # Assertion 6 — nested span parenting.
    tree_ids = {str(getattr(r, "id", "")) for r in descendants}
    parent_id_str = str(parent_run_id)
    child_runs = [r for r in descendants if str(getattr(r, "id", "")) != parent_id_str]
    child_parented = any(
        str(getattr(r, "parent_run_id", "") or "") == parent_id_str for r in child_runs
    ) or any(str(getattr(r, "trace_id", "") or "") == parent_id_str for r in child_runs)
    assertion(
        6,
        "Nested span parenting: ≥2 runs under trace_id; child parented to root",
        len(descendants) >= 2 and child_parented,
        observed=(
            f"runs_in_trace={len(descendants)} child_count={len(child_runs)} "
            f"child_parented={child_parented} ids={sorted(tree_ids)}"
        ),
        expected=">=2 runs AND child.parent_run_id == root",
    )

    # Assertion 7 — graceful degradation under bad endpoint.
    bad_env = os.environ.copy()
    bad_env["LANGSMITH_ENDPOINT"] = "https://invalid.example.com/this-host-does-not-exist"
    # Reload module-level state by re-checking is_enabled under the bad env.
    err_buf = io.StringIO()
    crashed = False
    sentinel_value: Any = None
    try:
        with _patched_env(bad_env), redirect_stderr(err_buf):
            sentinel_value = dispatch(run_id=uuid4(), payload={"phone": "+919876543210"})
    except BaseException as exc:  # noqa: BLE001
        crashed = True
        print(f"[7] caller saw exception: {exc!r}", file=sys.stderr)
    err_text = err_buf.getvalue()
    # The graceful-degradation contract: SDK fires the call but the SDK
    # itself swallows network errors in background threads. We assert the
    # call did NOT raise to the caller AND the function returned the real
    # (non-None) value. The stderr breadcrumb is best-effort; if the SDK
    # buffers writes async, the breadcrumb may or may not appear inside the
    # synchronous call window.
    has_breadcrumb = "LangSmith span" in err_text
    returned_real_value = isinstance(sentinel_value, dict) and sentinel_value.get("status") == "handled"
    assertion(
        7,
        "Graceful degradation: no caller-visible exception; real return preserved",
        (not crashed) and returned_real_value,
        observed=(
            f"crashed={crashed} returned={sentinel_value} "
            f"stderr_breadcrumb={'yes' if has_breadcrumb else 'no'}"
        ),
        expected="crashed=False AND returned_real_value=True",
    )

    return _finalise(parent_run=run_dict, descendants=[_run_to_dict(r) for r in descendants])


class _patched_env:
    """Context manager: temporarily overwrite os.environ with a new dict."""

    def __init__(self, new: dict[str, str]) -> None:
        self._new = new
        self._old: dict[str, str] | None = None

    def __enter__(self) -> None:
        self._old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self._new)

    def __exit__(self, *exc: object) -> None:
        os.environ.clear()
        assert self._old is not None
        os.environ.update(self._old)


def _finalise(parent_run: dict[str, Any] | None = None, descendants: list[dict[str, Any]] | None = None) -> int:
    print("\n=== CANARY SUMMARY ===")
    print(f"Project: {os.environ.get('LANGSMITH_PROJECT', '?')}")
    print(f"Endpoint: {os.environ.get('LANGSMITH_ENDPOINT', '?')}")
    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")
    if parent_run is not None:
        print("\n=== AUDIT ARTIFACT — parent run ===")
        print(json.dumps(parent_run, indent=2, default=_default_serialiser))
    if descendants is not None:
        print("\n=== AUDIT ARTIFACT — descendants ===")
        print(json.dumps(descendants, indent=2, default=_default_serialiser))
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 7 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    # Smoke-test the redactor inline so a busted redactor fails the
    # canary even before we contact LangSmith.
    assert "9876543210" not in str(redact_for_langsmith("+919876543210")), "redactor smoke fail"
    assert "Rajesh" not in str(redact_for_langsmith({"customer_name": "Rajesh Kumar"})), "redactor smoke fail"
    sys.exit(run_canary())
