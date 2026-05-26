---
task: VT-171
author: claudecode
ts: 2026-05-26T05:10:00+05:30
estimated_tokens: 125000
estimated_minutes: 110
classification: hot-fix
honors_decision: CL-56
supersedes_implementation_of: [VT-101]
---

## TL;DR

Replace LangSmith with Pydantic Logfire across `observability/`. Preserve VT-104 token-format byte-identical. Wire DBOS OTLP emission. Real-Logfire EU ingest + read-back in canary. Delete `langsmith.py` + remove every import site. Single PR ~125K (well under 180K ceiling). Three plan-ready questions below, all narrow.

## Approach

The redactor seam is unchanged — `redact()` in `privacy/pii_redactor.py` is canonical (VT-104), `observability/pii.py` is the thin wrapper (VT-104). VT-171 swaps only the OTel-sink side:

- `observability/langsmith.py` → DELETE. Module surface (`traceable_node`, `traceable_tool`, `trace_run`, `get_project_name`, `is_enabled`, `format_run_id_footer`) is replaced by Logfire equivalents in the new `observability/logfire.py`.
- `observability/pii.py` rename of public export: `redact_for_langsmith` → `redact_for_otel_span` (vendor-neutral). Old name kept as deprecated alias emitting `DeprecationWarning` for ONE cycle (removed in VT-172).
- `observability/__init__.py` re-exports updated; symmetric symbol set so existing call sites swap by import line, not by code change.
- `dbos_config.py` — add `enable_otlp: True` to `DBOSConfig` AFTER `configure_logfire()` has run; preserves the existing scheduled-workflow registration ordering CL-376 documented in `dbos_config.py:51`.
- `pyproject.toml` — remove `langsmith>=0.8,<0.9`; add `logfire>=4.0,<5`. Run `uv sync` (CL-58).
- Canary swaps LangSmith trace read-back to Logfire span read-back (Group B #6).

The redactor module + the `pipeline_log` writer + the cost dashboard + the privacy package + the reasoning-trace module are all UNCHANGED. The diff stays narrow.

### `observability/logfire.py` (new) — public surface

```python
def configure_logfire() -> None:
    """Idempotent. Calls logfire.configure(service_name=..., advanced=AdvancedOptions(base_url=...))
    + logfire.instrument_anthropic() + logfire.instrument_pydantic().
    EU base_url from LOGFIRE_BASE_URL (default https://logfire-eu.pydantic.dev).
    LOGFIRE_TOKEN unset → stderr warning + emit no spans (no-op disable)."""

def is_enabled() -> bool:
    """True iff configure_logfire() succeeded (LOGFIRE_TOKEN was present)."""

def traced_node(name: str) -> Callable[[F], F]:
    """Decorator replacing VT-101's traceable_node. Wraps fn in a Logfire span.
    Redacts args + return via redact_for_otel_span() BEFORE span captures them."""

def traced_tool(name: str) -> Callable[[F], F]:
    """Same shape for tool calls."""

def get_project_name() -> str:
    """LOGFIRE_PROJECT env, mirroring VT-101's get_project_name."""

def format_run_id_footer(run_id: UUID | str) -> str:
    """Same VT-101 footer markup; just under a different project URL."""

def trace_run(run_id: UUID | str) -> AbstractContextManager:
    """Logfire span context manager mirroring VT-101's trace_run for callers."""
```

All preserved-name symbols so call sites (`apps/team-orchestrator/src/orchestrator/agent/orchestrator_agent.py`, the supervisor, the canary regressions) keep their import shape — only the module path changes.

### `observability/pii.py` (rename)

```python
# canonical wrapper preserved
def redact_for_otel_span(value: Any, _depth: int = 0) -> Any:
    return redact(value, depth=_depth)

# deprecated alias — emits DeprecationWarning, removed in VT-172
def redact_for_langsmith(value: Any, _depth: int = 0) -> Any:
    warnings.warn(
        "redact_for_langsmith is deprecated; use redact_for_otel_span. "
        "Removed in VT-172.",
        DeprecationWarning, stacklevel=2,
    )
    return redact_for_otel_span(value, _depth)

# unchanged
redact_for_log = redact_for_otel_span
```

### `dbos_config.py` (modify)

```python
config: DBOSConfig = {
    "name": "team-orchestrator",
    "database_url": database_url,
    "enable_otlp": True,   # VT-171: routes DBOS spans through Logfire
}
```

`configure_logfire()` MUST run before `launch_dbos()` so the OTLP exporter is registered when DBOS starts emitting spans. Both invoked from `main.py:lifespan()`.

### `pyproject.toml`

- Remove: `langsmith>=0.8,<0.9`
- Add: `logfire>=4.0,<5`

### `.viabe/secrets/langsmith-dev.env`

Stays on disk (audit trail) but no longer sourced by canary loaders. Document deprecation in the file header via a one-line comment.

### CI gate (NEW — recommend)

`gate-no-langsmith-imports`: 3-line grep gate, parallel to existing `gate-no-deprecated-langgraph-imports`. Fails build if `from langsmith` or `import langsmith` appears anywhere under `apps/team-orchestrator/src/`. Structural CL-56 enforcement. Q2 below.

## File changes

- **NEW** `apps/team-orchestrator/src/orchestrator/observability/logfire.py`
- **MODIFY** `apps/team-orchestrator/src/orchestrator/observability/pii.py` — rename + deprecated alias
- **MODIFY** `apps/team-orchestrator/src/orchestrator/observability/__init__.py` — re-exports
- **DELETE** `apps/team-orchestrator/src/orchestrator/observability/langsmith.py`
- **MODIFY** `apps/team-orchestrator/src/dbos_config.py` — `enable_otlp: True`
- **MODIFY** `apps/team-orchestrator/src/main.py` — call `configure_logfire()` before `launch_dbos()`
- **MODIFY** `apps/team-orchestrator/pyproject.toml` — dep swap; `uv sync`
- **MODIFY** every import site: `apps/team-orchestrator/src/orchestrator/observability/log.py`, `tests/orchestrator/observability/test_pipeline_log.py`, `tests/orchestrator/observability/test_reasoning_trace.py`, `canaries/vt104_pii_redactor.py`, `canaries/README.md`
- **DELETE** `apps/team-orchestrator/tests/orchestrator/observability/test_langsmith.py` — replaced by `test_logfire.py`
- **NEW** `apps/team-orchestrator/tests/orchestrator/observability/test_logfire.py` — 5 pure tests per brief §7
- **NEW** `apps/team-orchestrator/canaries/vt171_logfire_migration.py` — 11 assertions
- **DELETE** `apps/team-orchestrator/canaries/vt101_langsmith.py` — replaced by VT-171's Group A regression (the VT-101 token contract is the assertion now; LangSmith trace-read assertion no longer applies)
- **MODIFY** `.github/workflows/ci.yml` — add `gate-no-langsmith-imports` job (conditional on Q2)
- **NEW** `.viabe/queue/VT-171/canary-run.log` — captured stdout for pre-merge-result

## Test plan

- `pytest tests/orchestrator/observability/test_logfire.py` — 5 pure pass.
- `pytest tests/` orchestrator-wide — 158+ pass / N skip, zero regression.
- `ruff check apps/team-orchestrator/src/orchestrator/observability` — clean.
- VT-171 canary 11/11 PASS, ≤60s wall-clock, cost < ₹1.
- **Condition-2-style regression** — re-run the VT-102 + VT-104 canary scripts on the new code (VT-101's standalone canary is being replaced by VT-171's Group A; the VT-101 token-contract assertion lives in Group A #1). Both VT-102 + VT-104 byte-identical assertion output preserved.

## Risks

1. **Logfire SDK API surface uncertainty (Q1).** I need to call:
   - `logfire.configure(service_name=..., token=..., advanced=AdvancedOptions(base_url=...))` — exact kwarg shape varies between v3.x and v4.x. Will verify via `logfire --help` + `from logfire import AdvancedOptions` introspection at PICKUP.
   - `logfire.instrument_anthropic()` — first-party per docs.
   - `logfire.instrument_pydantic()` — first-party per docs.
   - Span creation: `with logfire.span(name, **attrs): ...` is the documented API; `logfire.span(...)` returns a context manager.
   - Read-back API for Group B #6: the Logfire HTTPS query API requires a separate `LOGFIRE_READ_TOKEN` per current docs. **Q1 below — does our dev `LOGFIRE_TOKEN` carry read scope, or do we need Fazal to provision a separate read token?** Mitigation: if read token unavailable, Group B #6 falls back to verifying the local span buffer was non-empty (via `logfire.force_flush()` return) + Cowork manually verifies via the Logfire web UI in the merge audit.

2. **DBOS OTLP integration shape (Q3).** Brief says `enable_otlp=True` is the DBOSConfig knob. Need to verify the exact key name (`enable_otlp` vs `otel` vs `tracing`) per the dbos>=2.x SDK installed in this repo. If the key name differs, the migration still works (DBOS supports OTLP via env vars too: `OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS`). **Q3 below — confirm we use the DBOSConfig kwarg OR fall back to env-var-based config?** Mitigation: env-var fallback is cheaper to ship + survives DBOS SDK version drift.

3. **Deprecated alias emission contract.** `redact_for_langsmith` MUST emit `DeprecationWarning` AND still return correct output (regression for any straggler import). Tests cover this. **Single-cycle removal:** the alias gets deleted in VT-172 (which Cowork will queue as a follow-up; not in this row's scope).

4. **Anthropic `instrument_anthropic()` and existing manual @traceable wiring.** The orchestrator-agent calls Anthropic via `langchain_anthropic.ChatAnthropic` (per CL-249 LangChain wrapper around the Messages SDK). `logfire.instrument_anthropic()` patches the underlying `anthropic` SDK module — confirm it captures calls made through LangChain's wrapper. If not, Logfire's `instrument_langchain()` may be needed too. **Will verify at PICKUP via canary Group D #9.**

5. **Token format byte-identical contract.** VT-101 + VT-102 + VT-104 canaries (still in repo at `canaries/vt102_pipeline_log.py` + `canaries/vt104_pii_redactor.py`) assert exact `phone_tok_HEX` / `body_tok_HEX` / `<redacted:customer_name:len=N>` strings. NO changes to redactor in this PR. The renamed export `redact_for_otel_span` returns byte-identical output to the prior `redact_for_langsmith`. The deprecated alias is the explicit regression hook.

6. **"Too clean to be true" pattern.** VT-104 caught 3 bugs; same diligence here. If the canary passes 11/11 on first run with zero changes, manually verify (a) Logfire ingest is actually receiving spans (not just locally buffered), (b) DBOS span tree shows nested Anthropic child span (Group C #8), (c) deprecated alias actually emits `DeprecationWarning` to stderr.

7. **180K budget.** Estimate 125K (canary ~30K; module + tests + pii rewrite + delete cascade ~95K). Single PR fits with headroom.

## Plan-ready questions

### Q1 — Logfire read-back token

Group B #6 asserts spans are queryable via Logfire's HTTPS read API after ingest. Current docs require a separate `LOGFIRE_READ_TOKEN` per project (write tokens don't grant read scope). **Does our dev `.viabe/secrets/logfire-dev.env` `LOGFIRE_TOKEN` carry read scope, or does Fazal need to provision a separate read token?**

- **Recommend:** if separate token needed, Fazal provisions before merge (low blast radius). If unavailable mid-flight, the canary falls back to (a) `logfire.force_flush()` return-value verification + (b) Cowork manually verifies via the Logfire web UI in the audit pass — documented as a known gap in the supplement signal. This is a known-gap pattern, not a blocker.

### Q2 — Add `gate-no-langsmith-imports` CI gate

3-line grep gate, parallel to existing `gate-no-deprecated-langgraph-imports`. Fails build if `from langsmith` or `import langsmith` anywhere under `apps/team-orchestrator/src/`. Pays for itself by structurally enforcing CL-56 — re-shipping LangSmith silently like VT-101 did becomes impossible.

- **Recommend YES.**

### Q3 — DBOS OTLP config mechanism

`DBOSConfig` may not accept `enable_otlp` as a kwarg on the installed `dbos>=2.x` version (need to verify). DBOS supports OTLP via env vars (`OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS`) regardless of the DBOSConfig surface. **Two equivalent options:**

- **(A) DBOSConfig kwarg** — if the key name + value type is stable on installed dbos version. Verify at PICKUP via `from dbos import DBOSConfig; help(DBOSConfig)`.
- **(B) Env-var driven** — set `OTEL_EXPORTER_OTLP_ENDPOINT=https://logfire-eu.pydantic.dev/v1/traces` + `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ${LOGFIRE_TOKEN}` in `configure_logfire()` BEFORE `launch_dbos()`. Survives SDK version drift.

- **Recommend (B) — env-var driven.** Cheaper to ship + version-resilient. Verify in canary Group C #7 + #8 that DBOS workflow / step spans land in Logfire.

## Status

`.viabe/queue/VT-171/status` flipped `queued` → `planning` → `review`. Signalling plan-ready. Awaiting verdict on Q1/Q2/Q3; will proceed immediately on APPROVED.
