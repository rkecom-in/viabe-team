# ADR-0002: LangGraph orchestrator outer loop + Anthropic Agent SDK inner loop

**Status:** Accepted

## Context

Agent execution has two distinct shapes:

- **Outer loop** — multi-agent dispatch + state-machine semantics (supervisor → SR-Agent / Integration Agent / etc.); needs explicit, inspectable graph + checkpointing per-step
- **Inner loop** — single agent's tool-use cycle (LLM → tool call → tool result → LLM); cheaper, simpler, vendor-specific optimisations matter (prompt caching, structured-output JSON modes)

Two natural choices for each layer that don't slot together:

- **LangGraph** for orchestration — explicit nodes + edges + state; great for the outer loop
- **Anthropic Agent SDK** for tool-using agents — optimised for Claude; native prompt caching; tighter loop than LangGraph's reflection-on-toolcall pattern

## Considered Options

- **A.** Pure LangGraph (outer + inner) — uniform mental model but loses Anthropic prompt-cache optimisation (~6.9x cost reduction per VT-194)
- **B.** Pure Anthropic Agent SDK — loses LangGraph's explicit graph + tenant-scoped checkpoint isolation
- **C.** Split: LangGraph outer + Anthropic Agent SDK inner (chosen)

## Decision

**C.** LangGraph supervises the multi-agent dispatch (`supervisor.py` + `graph.py`). Inside each agent node, the agent runs its tool-use loop via `create_agent` (LangChain over Anthropic SDK) with `cache_control: {type: ephemeral}` blocks on the system prompt + tool registry. Outer-loop checkpoints land in PostgresSaver per tenant; inner-loop tool calls log via `@tool_step` decorator (VT-181) for cost attribution.

## Consequences

- (+) Outer loop is fully inspectable — operator can step through `pipeline_steps` and see the supervisor's routing decisions
- (+) Inner loop gets Anthropic's cache savings (VT-194 confirmed ~6.9x on system prompts)
- (+) Per-step checkpoint isolation per tenant via RLS on `langchain_checkpoint*` tables
- (−) Two different agent-loop mental models in the codebase — onboarding cost for new contributors
- (−) Tool-use observability spans two systems (`OrchestratorReasoningCallback` for outer, `@tool_step` for inner) — both write to `pipeline_steps` but the schema unifies them
- (−) LangGraph version churn requires periodic re-validation (CL-56 lesson: LangSmith was retired mid-flight)

## References

- CL-29 (LangGraph + Agent SDK split decision)
- CL-56 (LangSmith retired → Pydantic Logfire)
- CL-417 (canonical schema for pipeline_steps; both loops write the same shape)
- VT-181 (`@tool_step` decorator + `_observability_context` ContextVar)
- VT-194 (Anthropic prompt caching wiring; 6.9x cost reduction)
- VT-125 (`OrchestratorReasoningCallback` for outer-loop cost/token tracking)
