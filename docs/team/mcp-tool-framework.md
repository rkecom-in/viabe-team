# MCP Tool-Contract Framework (VT-39)

Reference for the framework every individual tool subtask (VT-5.2 … VT-5.13)
implements against. Module lives in
`packages/team-shared/python/team_shared/mcp/` so tools are testable in
isolation from the orchestrator.

## Contract

A tool is a class subclassing `MCPTool`. Each subclass declares four class
attributes + one method:

```python
from pydantic import BaseModel
from team_shared.mcp import MCPTool, ToolContext

class MyToolInput(BaseModel):
    ...   # NEVER a tenant_id field; the framework refuses it (Pillar 3)

class MyToolOutput(BaseModel):
    ...

class MyTool(MCPTool[MyToolInput, MyToolOutput]):
    name = "my_tool"
    description = "What this tool does, in one sentence."
    input_schema = MyToolInput
    output_schema = MyToolOutput

    def execute(self, ctx: ToolContext, inputs: MyToolInput) -> MyToolOutput:
        # Real work. ctx.tenant_id MUST flow through any DB read.
        ...
```

The framework wraps `execute` with the call lifecycle (below). Tools never
implement validation, telemetry, or error envelopes themselves.

## Call lifecycle (what the dispatch invokes)

`tool.call(ctx, raw_inputs)` → `ToolResult`:

1. **Input validation** — `input_schema.model_validate(raw_inputs)`.
   Failure → `ToolResult.error.code = INVALID_INPUT`; `execute` is NEVER
   called.
2. **Execute** — receives the validated input model. May raise; the
   framework traps to `EXECUTION_ERROR`.
3. **Output validation** — `output_schema.model_validate(...)` on the
   return value. Failure → `INVALID_OUTPUT`; the agent NEVER sees
   malformed payload data.
4. **Result** — `ToolResult` with `status=OK`, `data` from the validated
   output, and `latency_ms` populated.

## Tenant scoping (Pillar 3 — structural, non-negotiable)

The framework refuses to register a tool whose `input_schema` declares a
field named `tenant_id`. The refusal fires at class-definition time
(`__init_subclass__`); a malformed tool can't even import.

`ToolContext.tenant_id` is set by the orchestrator at the dispatch
boundary. Tools READ it; every DB read inside `execute` MUST pass
`ctx.tenant_id` through to the wrapper / scoped query.

Until VT-8.1 ships typed DB wrappers, `ctx.db_handle` carries
`orchestrator.db.tenant_connection` — the GUC-based RLS wrapper from
CL-122. The framework documents this as a bridge: when VT-8.1 lands,
`db_handle` switches to the richer typed factory and tool surface stays
the same.

## ToolContext

```python
@dataclass(frozen=True)
class ToolContext:
    tenant_id: UUID
    run_id: UUID
    agent_id: str
    parent_tool_call_id: UUID | None
    cost_budget_remaining_paise: int
    wallclock_remaining_ms: int
    db_handle: TenantConnectionFactory
```

`cost_budget_remaining_paise` and `wallclock_remaining_ms` are remaining
budgets the orchestrator passes per call so a tool can short-circuit
expensive work when the run is nearly over.

## ToolResult + ErrorEnvelope

```python
@dataclass
class ToolResult:
    status: ToolStatus              # OK | ERROR | RATE_LIMITED | UNAUTHORIZED | TIMEOUT
    data: dict | None               # output_schema.model_dump() on OK; None otherwise
    error: ErrorEnvelope | None     # populated on non-OK
    tokens_used: int = 0
    cost_paise: int = 0
    latency_ms: int = 0
    metadata: dict = field(default_factory=dict)   # structured k/v ONLY

@dataclass(frozen=True)
class ErrorEnvelope:
    code: ErrorCode                 # enum — no free strings
    message: str                    # ≤200 chars, NO PII
    retry_after_ms: int | None      # populated for RATE_LIMITED
```

The framework caps `ErrorEnvelope.message` at 200 chars defensively. PII
redaction is the tool author's responsibility at the construction site.

## Error codes

```
INVALID_INPUT           — input schema validation failed
INVALID_OUTPUT          — output schema validation failed
EXECUTION_ERROR         — execute() raised
RATE_LIMITED            — caller exceeded the tool's rate budget
UNAUTHORIZED            — caller's tenant has no access
TIMEOUT                 — wallclock exceeded the budget
DEPENDENCY_ERROR        — upstream service unavailable
TENANT_SCOPE_VIOLATION  — runtime scope check failed (defence-in-depth)
```

## Telemetry

Every tool call writes one row to `pipeline_steps` (the project's
`pipeline_log` surface — table created in VT-12.2 / migration 006):

- `step_kind = 'tool_call'`
- `input_envelope = {name, input_hash, is_llm_backed}`
- `output_envelope = {status, tokens_used, model_used?}`  (on success)
- `error_envelope = {code, message, model_used?}`  (on failure)
- `started_at` / `ended_at` / `duration_ms` / `cost_paise`

`PipelineStepsTelemetry` (in `team_shared.mcp.telemetry`) is the
production sink. Tests use `RecordingTelemetry` (in `test_harness.py`) to
assert event lifecycles without touching the DB.

LLM-backed tools MUST include `model_used` in the envelope — required for
cost-attribution audit (concept-team.md §8.4).

## LLM-backed tools

`MCPTool.is_llm_backed()` defaults to `False`. The override is rare and
must be justified: see [`llm-backed-tools-rationale.md`](llm-backed-tools-rationale.md).

The override site MUST carry a one-line rationale comment pointing back
to the rationale doc. CodeX rejects an LLM-backed tool that doesn't.

## Tool registry

`apps/team-orchestrator/src/orchestrator/agent/tool_registry.py`. Each
tool subtask (VT-5.2 onwards) lands a `register(MyTool)` call. The
registry provides:

- `register(tool_cls)` — idempotent against re-import; collision raises
- `get(name)` — KeyError on miss
- `all_tool_names()` — sorted
- `validate_subset(names)` — returns unknowns
- `llm_backed_in_subset(names)` — returns the LLM-backed members

The agent SDK's tool list is built from this registry at construction
time, filtered by the specialist's `tool_subset`.

## Test harness

`team_shared.mcp.run_tool_test(tool_cls, fixtures)` runs each fixture
through the tool's `call` and reports per-fixture pass/fail. Standard
shape: one positive fixture, one negative-path fixture (wrong tenant,
malformed input, simulated dependency failure).

Every tool's tests MUST import `run_tool_test` from `team_shared.mcp`. A
CI gate (`gate-vt39-tools-harness-import`) checks every file in
`apps/team-orchestrator/tests/orchestrator/agent/tools/` for the import.

## Writing a new tool — checklist

1. Subclass `MCPTool` with concrete `input_schema` / `output_schema`.
2. `name` is snake_case, globally unique across the registry.
3. `description` ≤80 chars — one sentence. The agent SDK shows it.
4. `execute` does the work. NO PII in logs; NO tenant_id from the agent;
   all DB reads through `ctx.db_handle(ctx.tenant_id)`.
5. Tests live in `apps/team-orchestrator/tests/orchestrator/agent/tools/`.
   File imports `run_tool_test`; runs positive + negative fixtures.
6. Add `register(MyTool)` to the registry's import-time block.
7. If LLM-backed: override `is_llm_backed()`; cite the rationale doc at
   the override site.

## Out of framework scope

- Per-tool rate-limit policy (tools implement their own; the framework
  provides the envelope shape).
- Tool versioning (Phase 1.5 — when a breaking change ships, name a new
  tool, retire the old one).
- Framework performance profiling (Phase 1.5).
- Agent SDK tool-registration wiring — the registry provides the list;
  whichever code builds the agent calls into the registry.
