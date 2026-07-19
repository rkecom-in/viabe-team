"""VT-374 §9 step harness — dep-less unit tests (Test-C, fix-contract §Test-C).

Contract requirements:
  - stub mode performs zero DB writes (SELECT-only against pipeline_steps)
  - --live exits nonzero (EXIT_REFUSED=3) on pause_deny or gate-manifest step
  - --live exits nonzero (EXIT_REFUSED=3) when app_environment sentinel != 'dev'
  - redaction warning printed to stderr for inputs_redacted_at_write entries

No dbos / langgraph at module import: this file runs in the dep-less CI smoke
(``uv run --no-project --with pytest pytest``). ``psycopg`` is not available
there either, so ``step_harness.py`` (which imports psycopg at module level) is
loaded via ``importlib.util.spec_from_file_location`` inside a module-scoped
fixture, with a psycopg stub pre-seeded in ``sys.modules``. The orchestrator
``src/`` is added to ``sys.path`` inside the fixture so ``_load_run_control``
finds ``orchestrator.run_control`` (stdlib-only by contract) without the
orchestrator's heavy deps.

This approach mirrors ``test_gate_manifest_grep.py``'s standalone file-load
pattern and avoids any module-level import of the harness or psycopg.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths — resolved once at module load (stdlib only, safe in dep-less smoke)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]  # …/viabe-team
_HARNESS_PATH = _REPO_ROOT / "scripts" / "step_harness.py"
_ORCH_SRC = _REPO_ROOT / "apps" / "team-orchestrator" / "src"


# ---------------------------------------------------------------------------
# Module-scoped fixture: load step_harness.py with psycopg stubbed out.
#
# Technique: register a MagicMock under 'psycopg' and 'psycopg.rows' in
# sys.modules BEFORE exec_module runs, so step_harness.py's top-level
# ``import psycopg`` binds to the mock and never imports the real C extension.
# The module is also registered under its own name before exec so that the
# dataclass machinery (which calls sys.modules[cls.__module__].__dict__)
# resolves correctly.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    """Load step_harness.py with psycopg stubbed; orchestrator src on sys.path."""
    assert _HARNESS_PATH.is_file(), (
        f"scripts/step_harness.py missing at {_HARNESS_PATH} — "
        "VT-374 deliverable required for Test-C"
    )
    assert _ORCH_SRC.is_dir(), (
        f"orchestrator src missing at {_ORCH_SRC} — "
        "run_control registry must be importable"
    )

    _psycopg_orig = sys.modules.get("psycopg")
    _psycopg_rows_orig = sys.modules.get("psycopg.rows")
    _harness_orig = sys.modules.get("step_harness")

    # Stub psycopg before loading the harness module.
    psycopg_stub = MagicMock()
    psycopg_rows_stub = MagicMock()
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = psycopg_rows_stub

    # Make orchestrator src importable (run_control is stdlib-only).
    if str(_ORCH_SRC) not in sys.path:
        sys.path.insert(0, str(_ORCH_SRC))

    try:
        spec = importlib.util.spec_from_file_location("step_harness", _HARNESS_PATH)
        assert spec is not None and spec.loader is not None, "spec_from_file_location failed"
        mod = importlib.util.module_from_spec(spec)
        # Register before exec so dataclass.__module__ resolves.
        sys.modules["step_harness"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        # Restore the original psycopg in sys.modules (don't leak the stub
        # into tests that actually want to import psycopg).
        if _psycopg_orig is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = _psycopg_orig
        if _psycopg_rows_orig is None:
            sys.modules.pop("psycopg.rows", None)
        else:
            sys.modules["psycopg.rows"] = _psycopg_rows_orig
        # Leave "step_harness" registered so subsequent fixture calls reuse it.
        if _harness_orig is None and "step_harness" not in sys.modules:
            pass  # was never there; fine

    # Sanity: psycopg stub is wired into the loaded module's psycopg reference.
    assert mod.psycopg is psycopg_stub  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Helpers — fake DB connection that records execute calls
# ---------------------------------------------------------------------------


class _FakeResult:
    """Minimal cursor-like result compatible with step_harness's .fetchone()/.fetchall()."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    """Fake psycopg connection context manager; records SQL statements executed."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.execute_calls: list[str] = []
        self._rows = rows or []

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def execute(self, sql: str, *args: Any) -> _FakeResult:
        self.execute_calls.append(sql.strip())
        return _FakeResult(self._rows)


def _make_step_row(harness: ModuleType, step_name: str, workflow_kind: str) -> Any:
    """Build a StepRow using the harness's dataclass (kind→step must be in DISPATCH)."""
    return harness.StepRow(
        id="00000000-0000-0000-0000-000000000001",
        run_id="00000000-0000-0000-0000-000000000002",
        tenant_id="00000000-0000-0000-0000-000000000003",
        step_seq=1,
        step_kind="webhook_received",
        step_name=step_name,
        status="completed",
        input_envelope={"message_type": "text"},
        output_envelope={"result": "recorded_output"},
    )


def _run_main(
    harness: ModuleType, argv: list[str]
) -> tuple[int, str, str]:
    """Call harness.main(argv); return (rc, stdout_text, stderr_text)."""
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        rc = harness.main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, buf_out.getvalue(), buf_err.getvalue()


# ---------------------------------------------------------------------------
# T-C1: stub mode performs zero DB writes
# ---------------------------------------------------------------------------


def test_stub_mode_zero_db_writes(harness: ModuleType) -> None:
    """Stub replay must never execute INSERT/UPDATE/DELETE/TRUNCATE; SELECT only (plan §9)."""
    conn = _FakeConn(rows=[])
    row = _make_step_row(harness, "dispatch_brain", "webhook_inbound")

    with (
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
        patch.object(harness, "_fetch_step_row", return_value=row),
    ):
        # Wire the psycopg stub so connect() returns our tracking connection.
        harness.psycopg.connect = lambda dsn, **kw: conn  # type: ignore[attr-defined]

        rc, stdout, _ = _run_main(
            harness,
            [
                "--run-id",
                row.run_id,
                "--step",
                "dispatch_brain",
                "--workflow-kind",
                "webhook_inbound",
            ],
        )

    assert rc == harness.EXIT_OK

    # The only query issued must be a SELECT (pipeline_steps row fetch).
    dml_prefixes = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
    dml_calls = [
        sql
        for sql in conn.execute_calls
        if sql.upper().startswith(dml_prefixes)
    ]
    assert not dml_calls, f"stub mode issued DML queries: {dml_calls}"

    # Sanity: the JSON output identifies stub mode.
    assert '"mode": "stub"' in stdout


# ---------------------------------------------------------------------------
# T-C2: stub mode output is the recorded output envelope verbatim
# ---------------------------------------------------------------------------


def test_stub_mode_replayed_output_is_recorded_output(harness: ModuleType) -> None:
    """Stub mode must return the recorded output_envelope as replayed_output unchanged."""
    row = _make_step_row(harness, "dispatch_brain", "webhook_inbound")

    with (
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
        patch.object(harness, "_fetch_step_row", return_value=row),
    ):
        harness.psycopg.connect = lambda dsn, **kw: _FakeConn()  # type: ignore[attr-defined]
        rc, stdout, _ = _run_main(
            harness,
            [
                "--run-id",
                row.run_id,
                "--step",
                "dispatch_brain",
                "--workflow-kind",
                "webhook_inbound",
            ],
        )

    assert rc == harness.EXIT_OK
    import json

    doc = json.loads(stdout)
    assert doc["replayed_output"] == row.output_envelope
    assert doc["mode"] == "stub"


# ---------------------------------------------------------------------------
# T-C3: --live exits EXIT_REFUSED on a pause_deny step (I6 compliance path)
# ---------------------------------------------------------------------------


def test_live_refused_for_pause_deny_step(harness: ModuleType) -> None:
    """--live must return EXIT_REFUSED (3) for a pause_deny=True step (I6).

    question_brain_compose is pause_deny (STEP-0 demotion — owner-inbound hot
    path with a blanket fail-open except that would swallow a raising hold).
    """
    with patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"):
        rc, _, stderr = _run_main(
            harness,
            [
                "--run-id",
                "00000000-0000-0000-0000-000000000002",
                "--step",
                "question_brain_compose",
                "--workflow-kind",
                "webhook_inbound",
                "--live",
            ],
        )

    assert rc == harness.EXIT_REFUSED, f"expected EXIT_REFUSED, got {rc}; stderr={stderr!r}"
    assert "pause_deny" in stderr or "I6" in stderr


# ---------------------------------------------------------------------------
# T-C4: --live exits EXIT_REFUSED for a step whose DISPATCH target is in GATE_MODULES
# ---------------------------------------------------------------------------


def test_live_refused_for_gate_manifest_module(harness: ModuleType) -> None:
    """--live must return EXIT_REFUSED (3) when the DISPATCH target module is a gate
    module (F14 — send/consent/approval surfaces are structurally non-replayable).

    Injects a synthetic DISPATCH entry targeting orchestrator.agents.customer_send
    (a real GATE_MODULES member) for an otherwise legitimate controllable step.
    """
    from orchestrator.run_control.gate_manifest import GATE_MODULES
    from orchestrator.run_control.registry import REGISTRY

    # Pick a step that is NOT pause_deny and HAS a real registry entry.
    step_key = ("agent_dispatch", "compose_drafts")
    real_entry = REGISTRY[step_key]
    assert not real_entry.pause_deny

    # Build a fake dispatch entry pointing at a gate module.
    gate_target_module = "orchestrator.agents.customer_send"
    assert gate_target_module in GATE_MODULES
    fake_dispatch = harness.DispatchEntry(
        target=f"{gate_target_module}:send_message",
        build_call=lambda row, env: ([], {}),
        note="injected for gate-module test",
    )

    patched_dispatch = {**harness.DISPATCH, step_key: fake_dispatch}
    with (
        patch.object(harness, "DISPATCH", patched_dispatch),
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
    ):
        rc, _, stderr = _run_main(
            harness,
            [
                "--run-id",
                "00000000-0000-0000-0000-000000000002",
                "--step",
                "compose_drafts",
                "--workflow-kind",
                "agent_dispatch",
                "--live",
            ],
        )

    assert rc == harness.EXIT_REFUSED, f"expected EXIT_REFUSED, got {rc}; stderr={stderr!r}"
    assert "gate" in stderr.lower() or "F14" in stderr or gate_target_module in stderr


# ---------------------------------------------------------------------------
# T-C5: --live exits EXIT_REFUSED when sentinel is not 'dev'
# ---------------------------------------------------------------------------


class _SentinelConn:
    """Fake psycopg connection that returns a configurable app_environment sentinel."""

    def __init__(self, sentinel: str | None) -> None:
        self._sentinel = sentinel

    def __enter__(self) -> "_SentinelConn":
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def execute(self, sql: str, *args: Any) -> _FakeResult:
        upper = sql.strip().upper()
        if "TO_REGCLASS" in upper:
            # sentinel present iff self._sentinel is not None
            val = "public.app_environment" if self._sentinel is not None else None
            return _FakeResult([{"to_regclass": val}])
        # SELECT name FROM app_environment
        if self._sentinel is not None:
            return _FakeResult([{"name": self._sentinel}])
        return _FakeResult([])


@pytest.mark.parametrize(
    "sentinel",
    [
        "prod",
        "staging",
        None,  # missing sentinel (no table)
    ],
    ids=["prod", "staging", "missing"],
)
def test_live_refused_when_sentinel_is_not_dev(
    harness: ModuleType, sentinel: str | None
) -> None:
    """--live must return EXIT_REFUSED (3) for any sentinel value that is not 'dev'
    (VT-362: the harness NEVER replays against a non-dev database)."""
    conn = _SentinelConn(sentinel)

    with (
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
    ):
        # source_fetch has a build_call (live-capable) and is not pause_deny or
        # in the gate manifest — so the only refusal arm should be the sentinel.
        harness.psycopg.connect = lambda dsn, **kw: conn  # type: ignore[attr-defined]

        rc, _, stderr = _run_main(
            harness,
            [
                "--run-id",
                "00000000-0000-0000-0000-000000000002",
                "--step",
                "source_fetch",
                "--workflow-kind",
                "auto_discovery",
                "--live",
            ],
        )

    assert rc == harness.EXIT_REFUSED, (
        f"sentinel={sentinel!r}: expected EXIT_REFUSED, got {rc}; stderr={stderr!r}"
    )
    # The refusal message should mention VT-362 or sentinel or 'dev'
    assert any(kw in stderr for kw in ("VT-362", "sentinel", "dev", "stamp")), stderr


def test_live_proceeds_with_dev_sentinel_until_step_execution(
    harness: ModuleType,
) -> None:
    """Sanity: with a 'dev' sentinel the guard passes — refusal only comes from
    the step having no live adapter (build_call=None for dispatch_brain), which
    proves _assert_dev_sentinel itself did not refuse."""
    # dispatch_brain has no build_call → refused BEFORE sentinel check (gate order: first
    # refuse_live_gates, then connect + sentinel). Use source_fetch (has build_call) but
    # patch _fetch_step_row to avoid a real DB call; the step execution itself will fail
    # (module not importable in dep-less), so we only test that we get past the sentinel.
    conn = _SentinelConn("dev")
    row = _make_step_row(harness, "source_fetch", "auto_discovery")

    sentinel_check_called = []

    original_assert = harness._assert_dev_sentinel  # type: ignore[attr-defined]

    def tracking_sentinel(c: Any) -> None:
        sentinel_check_called.append(True)
        original_assert(c)

    with (
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
        patch.object(harness, "_assert_dev_sentinel", side_effect=tracking_sentinel),
        patch.object(harness, "_fetch_step_row", return_value=row),
        patch.object(harness, "_invoke_live", return_value={"live": True}),
    ):
        harness.psycopg.connect = lambda dsn, **kw: conn  # type: ignore[attr-defined]
        rc, stdout, _ = _run_main(
            harness,
            [
                "--run-id",
                row.run_id,
                "--step",
                "source_fetch",
                "--workflow-kind",
                "auto_discovery",
                "--live",
            ],
        )

    assert sentinel_check_called, "_assert_dev_sentinel was never called with 'dev' sentinel"
    # The run proceeds to step execution (rc=0 since _invoke_live is patched)
    assert rc == harness.EXIT_OK, f"expected EXIT_OK after dev sentinel, got {rc}"


# ---------------------------------------------------------------------------
# T-C6: redaction warning printed to stderr for inputs_redacted_at_write entries
# ---------------------------------------------------------------------------


def test_redaction_warning_printed_for_redacted_at_write_step(
    harness: ModuleType,
) -> None:
    """A loud warning must go to stderr when inputs_redacted_at_write=True (plan §9).

    dispatch_brain is the only v1 step registered inputs_redacted_at_write=True
    (the webhook_received envelope has body popped + phone hashed at write, STEP-0 §3.2).
    """
    from orchestrator.run_control.registry import REGISTRY

    entry = REGISTRY[("webhook_inbound", "dispatch_brain")]
    assert entry.inputs_redacted_at_write, (
        "test assumption: dispatch_brain must have inputs_redacted_at_write=True"
    )

    row = _make_step_row(harness, "dispatch_brain", "webhook_inbound")

    with (
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
        patch.object(harness, "_fetch_step_row", return_value=row),
    ):
        harness.psycopg.connect = lambda dsn, **kw: _FakeConn()  # type: ignore[attr-defined]
        rc, _, stderr = _run_main(
            harness,
            [
                "--run-id",
                row.run_id,
                "--step",
                "dispatch_brain",
                "--workflow-kind",
                "webhook_inbound",
            ],
        )

    assert rc == harness.EXIT_OK
    assert "WARNING" in stderr, f"redaction warning not printed; stderr={stderr!r}"
    assert "inputs_redacted_at_write" in stderr or "PII-REDACTED" in stderr or "REDACTED" in stderr


def test_no_redaction_warning_for_non_redacted_step(harness: ModuleType) -> None:
    """Steps without inputs_redacted_at_write=True must NOT print the redaction warning."""
    from orchestrator.run_control.registry import REGISTRY

    # generate_validate is a non-redacted controllable step.
    entry = REGISTRY[("plan_generate", "generate_validate")]
    assert not entry.inputs_redacted_at_write

    row = _make_step_row(harness, "generate_validate", "plan_generate")

    with (
        patch.object(harness, "_resolve_dsn", return_value="postgresql://fake/db"),
        patch.object(harness, "_fetch_step_row", return_value=row),
    ):
        harness.psycopg.connect = lambda dsn, **kw: _FakeConn()  # type: ignore[attr-defined]
        _, _, stderr = _run_main(
            harness,
            [
                "--run-id",
                row.run_id,
                "--step",
                "generate_validate",
                "--workflow-kind",
                "plan_generate",
            ],
        )

    assert "WARNING" not in stderr, (
        f"unexpected redaction warning for non-redacted step; stderr={stderr!r}"
    )


# ---------------------------------------------------------------------------
# T-C7: _refuse_live_gates unit — all three refusal arms
# ---------------------------------------------------------------------------


def test_refuse_live_gates_pause_deny(harness: ModuleType) -> None:
    """pause_deny=True must raise HarnessRefusal regardless of gate/adapter."""
    from orchestrator.run_control.gate_manifest import GATE_MODULES
    from orchestrator.run_control.registry import REGISTRY

    entry = REGISTRY[("webhook_inbound", "question_brain_compose")]
    dispatch = harness.DISPATCH[("webhook_inbound", "question_brain_compose")]

    with pytest.raises(harness.HarnessRefusal, match="pause_deny"):
        harness._refuse_live_gates(entry, dispatch, GATE_MODULES)


def test_refuse_live_gates_gate_module(harness: ModuleType) -> None:
    """A DISPATCH target whose module is in GATE_MODULES must raise HarnessRefusal."""
    from orchestrator.run_control.gate_manifest import GATE_MODULES
    from orchestrator.run_control.registry import REGISTRY

    gate_module = "orchestrator.agents.customer_send"
    assert gate_module in GATE_MODULES

    entry = REGISTRY[("agent_dispatch", "execute_item")]  # not pause_deny
    fake_dispatch = harness.DispatchEntry(
        target=f"{gate_module}:send_fn",
        build_call=lambda row, env: ([], {}),
        note="test",
    )

    with pytest.raises(harness.HarnessRefusal, match="gate"):
        harness._refuse_live_gates(entry, fake_dispatch, GATE_MODULES)


def test_refuse_live_gates_no_adapter(harness: ModuleType) -> None:
    """build_call=None must raise HarnessRefusal (no live adapter available)."""
    from orchestrator.run_control.gate_manifest import GATE_MODULES
    from orchestrator.run_control.registry import REGISTRY

    entry = REGISTRY[("agent_dispatch", "execute_item")]
    dispatch = harness.DISPATCH[("agent_dispatch", "execute_item")]
    assert dispatch.build_call is None

    with pytest.raises(harness.HarnessRefusal, match="no live adapter"):
        harness._refuse_live_gates(entry, dispatch, GATE_MODULES)


# ---------------------------------------------------------------------------
# T-C8: _assert_dev_sentinel unit — direct function tests
# ---------------------------------------------------------------------------


def test_assert_dev_sentinel_accepts_dev(harness: ModuleType) -> None:
    """_assert_dev_sentinel must NOT raise for a 'dev'-stamped DB."""
    conn = _SentinelConn("dev")
    # Should complete without raising.
    buf = io.StringIO()
    old_err = sys.stderr
    sys.stderr = buf
    try:
        harness._assert_dev_sentinel(conn)
    finally:
        sys.stderr = old_err
    assert "dev" in buf.getvalue().lower()


@pytest.mark.parametrize(
    "sentinel,match_kw",
    [
        ("prod", "prod"),
        ("staging", "staging"),
        (None, "sentinel"),
    ],
    ids=["prod", "staging", "missing"],
)
def test_assert_dev_sentinel_refuses_non_dev(
    harness: ModuleType, sentinel: str | None, match_kw: str
) -> None:
    """_assert_dev_sentinel must raise HarnessRefusal for any non-dev sentinel."""
    conn = _SentinelConn(sentinel)
    with pytest.raises(harness.HarnessRefusal, match=re_escape_enough(match_kw)):
        harness._assert_dev_sentinel(conn)


def re_escape_enough(s: str) -> str:
    """Minimal escaper: periods only (used in match= args below)."""
    return s.replace(".", r"\.")


# ---------------------------------------------------------------------------
# T-C9: _parse_pins unit
# ---------------------------------------------------------------------------


def test_parse_pins_json_values(harness: ModuleType) -> None:
    """JSON-parseable values are decoded; non-JSON values are kept as raw strings."""
    pins = harness._parse_pins(["model=gpt-4", "limit=3", "flag=true", "skip_sources=[1,2]"])
    assert pins == {"model": "gpt-4", "limit": 3, "flag": True, "skip_sources": [1, 2]}


def test_parse_pins_bad_format_raises(harness: ModuleType) -> None:
    """A pair without '=' must raise HarnessError (exit-4 class)."""
    with pytest.raises(harness.HarnessError):
        harness._parse_pins(["noequals"])


# ---------------------------------------------------------------------------
# T-C10: --list exits EXIT_OK and prints the step table
# ---------------------------------------------------------------------------


def test_list_exits_ok_and_prints_table(harness: ModuleType) -> None:
    """--list must exit 0 and print all DISPATCH entries in table form."""
    rc, stdout, _ = _run_main(harness, ["--list"])
    assert rc == harness.EXIT_OK
    # All known (workflow_kind, step_name) pairs from DISPATCH appear.
    for wk, sn in harness.DISPATCH:
        assert wk in stdout or sn in stdout, f"({wk}, {sn}) missing from --list output"


# ---------------------------------------------------------------------------
# T-C11: missing --run-id or --step exits EXIT_USAGE (2)
# ---------------------------------------------------------------------------


def test_missing_run_id_exits_usage(harness: ModuleType) -> None:
    rc, _, stderr = _run_main(harness, ["--step", "dispatch_brain"])
    assert rc == harness.EXIT_USAGE


def test_missing_step_exits_usage(harness: ModuleType) -> None:
    rc, _, _ = _run_main(
        harness, ["--run-id", "00000000-0000-0000-0000-000000000002"]
    )
    assert rc == harness.EXIT_USAGE


# ---------------------------------------------------------------------------
# T-C12: ambiguous step (appears in multiple workflow_kinds) requires --workflow-kind
# ---------------------------------------------------------------------------


def test_ambiguous_step_exits_usage_without_workflow_kind(harness: ModuleType) -> None:
    """If a step_name appears in multiple workflow_kinds, --workflow-kind is required."""
    # We need to check whether any step_name appears in >1 kind in DISPATCH.
    from collections import defaultdict

    by_step: dict[str, list[str]] = defaultdict(list)
    for wk, sn in harness.DISPATCH:
        by_step[sn].append(wk)
    ambiguous = [sn for sn, kinds in by_step.items() if len(kinds) > 1]

    if not ambiguous:
        pytest.skip("no ambiguous step names in current DISPATCH — skip until one exists")

    rc, _, stderr = _run_main(
        harness,
        ["--run-id", "00000000-0000-0000-0000-000000000002", "--step", ambiguous[0]],
    )
    assert rc == harness.EXIT_USAGE
    assert "--workflow-kind" in stderr or "kinds" in stderr
