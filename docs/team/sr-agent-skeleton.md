# Sales Recovery Agent — SDK Skeleton (VT-32)

Reference for the real specialist agent at
`apps/team-orchestrator/src/orchestrator/agent/sales_recovery.py`. Phase 1
plumbing only — placeholder prompt, no real tools. The real prompt and the
real `SalesRecoveryContext` bundle land in later subtasks.

## Tier-2 contract

`run_sales_recovery_agent(context: SalesRecoveryContext) -> AgentResult`.

The specialist receives a typed context, runs an agent loop on the Anthropic
Messages API, and returns a typed `AgentResult`. It MUST NOT touch the
database, send WhatsApp messages, or mutate LangGraph state directly. Those
side effects live with the orchestrator.

## Anthropic SDK choice

Hand-written loop on the **`anthropic` Messages SDK** (Python). The
"Anthropic Agent SDK" wording in the original VT-32 spec is generic-descriptive,
not a product reference — the `claude-agent-sdk` package (CLI-bundled) is
explicitly rejected because (a) it would put Node + the Claude Code CLI on the
Railway image, (b) it is built for CLI/coding agents, and (c) the loop must be
ours so VT-35's hard-limit enforcers attach to known seams.

**Pin**: `anthropic==0.103.1`. Same `==` pin policy as langgraph / langchain
(CL-147). Bumps are Type 2 governance — the loop reads `usage`,
`stop_reason`, `content` block shapes, and `thinking` config; behaviour
changes in any of those are agent-visible.

The dep was previously unpinned (`"anthropic"` with no version constraint —
dep-audit 2026-05-18 finding). VT-32 pins the previously-unpinned baseline.

## Streaming vs non-streaming (settled in VT-32)

The loop uses **non-streaming** `client.messages.create(...)`. The
non-streaming `Message` response populates `.usage` (`input_tokens`,
`output_tokens`) at the per-turn boundary, which is what VT-35's token
meter needs. No streaming machinery is required for the placeholder
canary, and staying non-streaming keeps the per-turn seam
(`_run_one_turn`) a single round-trip — simpler for VT-35 to
instrument.

If a later subtask needs token-by-token deltas (e.g. for an aborting
mid-response enforcer), the seam can be switched to
`messages.stream()` without changing the enforcer attach points.

## Extended thinking (deferred)

VT-32 deliberately ships with `thinking` NOT wired into the
`messages.create` call. The placeholder system prompt asks the model
to emit one JSON line — zero reasoning is required, and a 16K
thinking budget on a 1024-token max_tokens response is a 400 from the
API (`budget_tokens` must be strictly less than `max_tokens`).

Whether the real sales-recovery agent uses extended thinking, and at
what budget, is a **VT-4.2-era per-turn reasoning decision** — it
interacts with the prompt design AND with VT-35's depth tracker. When
that subtask lands, re-introduce:

```python
thinking={"type": "enabled", "budget_tokens": N}  # N < _MAX_OUTPUT_TOKENS_PER_TURN
```

The `N < _MAX_OUTPUT_TOKENS_PER_TURN` invariant is API-enforced; the
PR re-adding the parameter MUST also raise `_MAX_OUTPUT_TOKENS_PER_TURN`
to accommodate the chosen budget plus the actual response length.

## Per-response cap vs run-level hard limit

Two distinct token numbers — do not conflate:

| Constant | Where | Meaning | Wired to |
|---|---|---|---|
| `_MAX_OUTPUT_TOKENS_PER_TURN = 1024` | `sales_recovery.py` | "max length of ONE response" — the Messages API parameter | `messages.create(max_tokens=...)` |
| `_RUN_LEVEL_TOKEN_HARD_LIMIT = 80_000` | `sales_recovery.py` | The VT-35 cumulative run-level ceiling | **NOT** passed to any SDK call — VT-35 enforces externally |

The 80K figure is the AgentResult/HardLimitAxis semantics; the 1024
figure is the SDK call parameter. Passing 80K to `messages.create`
trips the SDK's non-streaming 10-minute timeout guard (verified
2026-05-20 canary failure #1); the brief's earlier conflation of the
two caused that defect. The follow-up `thinking.budget_tokens > max_tokens`
400 (canary failure #2) is the same class of mistake: spec-numbers
copied into SDK params without sanity-checking for the placeholder.

## VT-35 hard-limit enforcement

Four independent enforcers + a first-wins `CancellationContext` are wired
into the loop. The orchestrator measures; the agent has no visibility into
its own usage (Pillar 1). Termination is unilateral — no soft warnings.

| Enforcer | Budget | Where it attaches | What signals cancel |
|---|---|---|---|
| `TokenMeter` | 80,000 (cumulative input+output) | post-`_run_one_turn` via `.usage` | total > 80_000 |
| `ToolCounter` | 25 dispatches | per call to `_dispatch_tool` | count > 25 (26th trips) |
| `DepthTracker` | 8 think→tool cycles | per `_dispatch_tool` + post-`_run_one_turn` reasoning | depth > 8 (9th trips) |
| `WallclockTimer` | 300s | turn-boundary deadline check + per-turn HTTP timeout (60s) | `now > start + 300s` |

All budgets reset per invocation — no cross-dispatch carry. The four
constants (`_RUN_LEVEL_TOKEN_HARD_LIMIT`, `TOOL_CALL_HARD_LIMIT`,
`DEPTH_HARD_LIMIT`, `WALL_CLOCK_HARD_LIMIT_S`) are Type-3 commitments;
`.github/workflows/ci.yml` `gate-vt35-hard-limit-constants` greps each
literal so any value change fails CI.

On cancellation:
1. The first signal wins (`CancellationContext` is idempotent).
2. The loop breaks at the next check point (no orphan API call).
3. `AgentResult.status = 'terminated'`, `terminated_by = <HardLimitAxis>`,
   `terminated_reason = <enforcer's message>`.
4. A `FailureRecord(AGENT_HARD_LIMIT_BREACH)` with `metadata.axis` is
   emitted via `error_router.route_failure` — the router escalates per
   the VT-29 strategy table (HIGH severity → owner, plus persistence to
   `pipeline_steps`).
5. `cost_paise` STILL accrues — the API spend already happened.

### Sync vs async (VT-35, option b)

The loop is synchronous. Blocking calls cannot be cleanly interrupted
from an asyncio timer, so VT-35 ships **option (b)**: a monotonic-clock
deadline checked at each turn boundary PLUS a per-turn HTTP timeout
(`PER_TURN_HTTP_TIMEOUT_S = 60.0`) passed to `messages.create(timeout=)`.

The combination bounds both axes of "run too long":
- many fast turns accumulating past 300s → turn-boundary deadline check
- one hung turn taking >5min → httpx-level per-call timeout

No asyncio task, no separate thread, no zombies — the WallclockTimer is
a plain object that goes out of scope when `run_sales_recovery_agent`
returns. If the loop later goes async (e.g. for token-by-token
streaming), only the WallclockTimer's mechanism changes; the seam
interfaces stay identical.

## Model resolution

`apps/team-orchestrator/config/models.yaml` maps each agent to two model ids:

| Slot | Model | When |
|---|---|---|
| `production` | `claude-opus-4-7` | `VIABE_ENV=production` |
| `test` | `claude-haiku-4-5` | everything else (default) |

`_resolve_model(agent_name)` in `sales_recovery.py` reads this config. The
agent code NEVER hardcodes a model string. Default fallback is the `test`
slot (Haiku) — never silently fall through to Opus in development.

**Hard-limit validation lives on Opus.** VT-35's behaviour calibration is
against the production model; do not let Haiku leak into that path — the
budgets and step-count behaviour differ.

## Cost attribution (Phase 1)

`apps/team-orchestrator/src/orchestrator/agent/cost.py`. Deterministic
token → paise table:

```
cost_paise = round(
    (input_tokens * paise_per_M_input + output_tokens * paise_per_M_output)
    / 1_000_000
)
```

| Model | Input ($/M) | Output ($/M) | paise/M input | paise/M output |
|---|---|---|---|---|
| `claude-opus-4-7` | $15 | $75 | 127,500 | 637,500 |
| `claude-haiku-4-5` | $1 | $5 | 8,500 | 42,500 |

Conversion assumption: **₹85 / USD**, **as of 2026-05-20** (Phase 1 fixed).
100 paise = 1 INR.

Single source of truth: `_USD_TO_INR` in
`apps/team-orchestrator/src/orchestrator/agent/cost.py`. The doc table
above is derived from that constant — when refreshing, update the
constant first, then bump the as-of date here.

These are budget-attribution numbers, not billing numbers. Anthropic invoices
in USD on cache-aware totals; full billing reconciliation lands in a later
observability subtask. Phase 1 accuracy is sufficient for run-level
cost_paise on `pipeline_steps` / `pipeline_runs.cost_paise`.

**Refresh policy**: update `RATES` (and `_USD_TO_INR` when applicable) and
bump the as-of date above when (a) Anthropic changes a list price,
(b) the FX assumption drifts by more than ~5% from spot, or (c) a new model
is added.

## Cost on terminated runs

`cost_paise` accrues even when a run is terminated by a hard-limit enforcer
(VT-35 hard rule). The API spend happened; refunds are not a thing. The
cost number on a terminated run reflects what was consumed up to the cancel
point.

## Canary

The real-API canary (`test_canary_real_haiku_run_returns_placeholder_status`)
runs against `claude-haiku-4-5`, env-gated on
`VIABE_RUN_AGENT_CANARY=1` + `ANTHROPIC_API_KEY`. Skipped in CI; Fazal
triggers it manually once before merge. This is the only real API call
VT-32 makes.

## Status enum

`AgentResult.status: Literal['completed','terminated','refused','invalid','placeholder']`.

`placeholder` is included so the canary path has a clean terminal state
(the placeholder prompt emits `{"status": "placeholder"}` and the loop
exits cleanly). No existing orchestrator enum covered this case — VT-32
introduces the field as part of the AgentResult contract.

`terminated_by` reuses `failures.HardLimitAxis` (VT-29 / CL-242). VT-35
populates this; VT-32 just ensures the dataclass accepts every axis member.

## Dispatch wiring (out of scope for VT-32)

`sales_recovery_node` is exported in `agent/sales_recovery_node.py`. The
supervisor graph (`supervisor.py`) STILL routes through
`build_stub_sales_recovery_agent` from `sales_recovery_stub.py`. Switching
the dispatch call site is a separate subtask — VT-32 just makes the real
node available.

## Imports of the stub

Current call sites of `build_stub_sales_recovery_agent` /
`hardcoded_campaign_plan`:

- `apps/team-orchestrator/src/orchestrator/supervisor.py:29-31` (imports)
- `apps/team-orchestrator/src/orchestrator/supervisor.py:70` (constructs stub)
- `apps/team-orchestrator/src/orchestrator/supervisor.py:94` (parse-fallback)

The stub is NOT modified or deleted by VT-32.
