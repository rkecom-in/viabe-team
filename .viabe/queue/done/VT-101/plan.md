---
task: VT-101
author: claudecode
ts: 2026-05-25T20:17:34+05:30
estimated_tokens: 110000
estimated_minutes: 75
---

## Approach

Wrap the existing orchestrator pipeline in LangSmith tracing using the `run_id` (UUID v4) that's **already plumbed** throughout the codebase (`context_builder.py`, `collapse.py`, `runner.py`, etc.) as the canonical LangSmith trace ID — satisfying Pillar 8's "one run_id namespace" without generating new IDs. New module `orchestrator/observability/langsmith.py` holds: env-driven client init (`LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` = `viabe-team-dev` | `viabe-team-prod`), `@traceable_node` and `@traceable_tool` decorators thinly wrapping LangSmith's own `@traceable` with a `run_id`-as-trace-id convention, a `trace_run(run_id, name)` async context manager for non-LangGraph paths (webhook handler, DBOS workflow steps), and a `redact_for_langsmith(value)` inline PII utility (~30 lines: phone-number-pattern → SHA256[:12] using existing `utils.phone_token.hash_phone`; message bodies → `hash_body(b)`; customer names from known field paths → `<name:N chars>` tokens). The decorator wraps tool input/output through `redact_for_langsmith` BEFORE the value crosses into LangSmith — this is the bypass-impossible point. Graceful degradation via `langsmith.utils.tracing_is_enabled()` short-circuit plus a try/except around every span creation that logs to stderr but never raises. LangGraph's native `LANGCHAIN_TRACING_V2=true` + `LANGCHAIN_PROJECT=<project>` env vars wire up node tracing automatically; we add explicit decorators only on the few places LangGraph can't see (DBOS step bodies, MCP tool handlers, Telegram dispatcher, the Twilio webhook handler at `api/twilio_ingress.py`).

## File changes

- **NEW `apps/team-orchestrator/src/orchestrator/observability/__init__.py`** — empty.
- **NEW `apps/team-orchestrator/src/orchestrator/observability/langsmith.py`** — client init, `traceable_node` / `traceable_tool` decorators, `trace_run(run_id, name)` async ctx manager, `is_enabled()` helper, env-resolver `get_project_name() -> "viabe-team-dev" | "viabe-team-prod"`.
- **NEW `apps/team-orchestrator/src/orchestrator/observability/pii.py`** — `redact_for_langsmith(value: Any) -> Any` (recurses dict/list/str). Inline phone-regex → existing `utils.phone_token.hash_phone`; body-text → SHA256[:16] with `body:` prefix; known PII keys (`name`, `customer_name`, `email`) → `<redacted:type>` tokens. ~50 lines. Self-contained; will be subsumed by VT-104 later (Cowork heads-up acknowledged).
- **NEW `apps/team-orchestrator/tests/orchestrator/observability/__init__.py`** + **`test_langsmith.py`** — pytest. 6 cases per brief acceptance criteria, all with mocked LangSmith client (no real API calls):
  1. `test_dispatch_generates_traceable_span_with_run_id` — fake `WebhookEvent` → run through `runner.run_orchestrator_workflow` with patched LangSmith client; assert one root span carries `metadata.run_id == webhook_event.run_id`.
  2. `test_nested_spans_inherit_parent_run_id` — orchestrator span → agent span → tool span; assert all three spans share root `trace_id == run_id`.
  3. `test_run_id_propagates_to_pipeline_log_and_telegram_footer` — synthetic run; assert `pipeline_steps.run_id` column matches; assert outbound Telegram message body contains `run_id=<value>` footer.
  4. `test_redaction_applied_before_langsmith_send` — synthetic prompt body `"Hi, my number is +91 98765 43210"`; capture LangSmith span input; assert no `9876543210` substring; assert SHA-hashed token present.
  5. `test_project_isolation_dev_vs_prod` — patch env `LANGSMITH_PROJECT=viabe-team-prod`; assert `get_project_name() == "viabe-team-prod"`; default `viabe-team-dev`.
  6. `test_langsmith_failure_does_not_crash_pipeline` — `langsmith.Client.create_run` raises `RuntimeError`; assert orchestrator workflow still completes; assert exception logged to stderr but not re-raised.
- **MODIFY `apps/team-orchestrator/pyproject.toml`** — add `"langsmith>=0.8,<0.9"` to dependencies (already transitively present at 0.8.5; declaring explicit per dep-hygiene).
- **MODIFY `apps/team-orchestrator/src/orchestrator/runner.py`** — wrap `open_run` / `pre_filter_step` / `langgraph_step` / `close_run` DBOS steps with `traceable_node(name="...")` decorator; pass `run_id` as `trace_id` parameter.
- **MODIFY `apps/team-orchestrator/src/orchestrator/api/twilio_ingress.py`** — wrap the inbound webhook handler entry in `trace_run(event.run_id, "webhook.twilio")` context manager.
- **MODIFY `apps/team-orchestrator/src/orchestrator/agent/sales_recovery.py`** (if it exists per VT-32) — decorate the `run_agent()` entry with `traceable_node("agent.sales_recovery")`.
- **MODIFY `apps/team-orchestrator/.env.example`** — add `LANGSMITH_API_KEY=`, `LANGSMITH_PROJECT=viabe-team-dev`, `LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_PROJECT=viabe-team-dev`. (LangGraph reads the `LANGCHAIN_*` set natively; the `LANGSMITH_*` set is for our explicit decorators. Both names point at the same backend.)
- **NEW `docs/team/langsmith-pii-policy.md`** — short doc: which fields are redacted, hash convention, how to add new redaction rules, why bypass is mechanically blocked at decorator boundary. ~50 lines.

## Test plan

Six behavioral pytest cases above. All run with `LANGSMITH_API_KEY` UNSET in CI (the graceful-degradation case 6 proves no-key still works). Real-API canary is OUT — brief doesn't ask for one and we lack a budget-bounded LangSmith test project; the mocked-client tests cover the wire-level contract. Local validation: `cd apps/team-orchestrator && .venv/bin/pytest tests/orchestrator/observability/ -v` (target: 6 passed), `.venv/bin/ruff check src/orchestrator/observability src/orchestrator/runner.py src/orchestrator/api/twilio_ingress.py` (clean), `.venv/bin/python -m mypy --strict src/orchestrator/observability/` (brief acceptance criterion).

## Risks

1. **Brief artifacts vs current repo state.** Brief uses obsolete `apps/team/` paths, references retired CoderC/CoderX reviewers, mentions a `dev` merge target that doesn't exist, and references VT-Observability-Cost in the PR title (won't pass the `pr-title` regex `\(VT-[0-9A-Za-z][0-9A-Za-z.]*(-fix-[0-9]+)?\)$` because the segment contains a hyphen — same class of failure as VT-AGENTSDK-LOOP). **Resolution in this plan:** paths corrected to `apps/team-orchestrator/`; PR title will end `(VT-101)`; merge target `main`; reviewers Clau + Fazal per current `[[reviewer-terminology]]` memory. Surfaced to Cowork in `plan-ready` so it can be ack'd before implementation, not after.

2. **VT-12.4 PII redactor not built.** Cowork's heads-up offered Option 1 (inline ~20-line utility) vs Option 2 (block on VT-104). Plan takes Option 1 with the larger ~50-line scope (phone + body + named-key tokenization) — the brief's PII discipline is non-negotiable and a 20-line version doesn't cover named keys. Future VT-104 swaps `redact_for_langsmith` for a richer redactor; the call sites stay the same.

3. **LangGraph's native LangSmith integration overlaps with our explicit decorators.** Setting `LANGCHAIN_TRACING_V2=true` auto-traces every LangGraph node — our manual `@traceable_node` decorators on DBOS steps + handlers fire alongside. Worst case: duplicate spans for nodes that are both DBOS-wrapped and LangGraph-wrapped. Mitigation: explicit decorators only on code paths LangGraph can't reach (webhook handler, DBOS step bodies that don't invoke the graph). I'll verify zero duplicate-span output in the nested-spans test (#2).

4. **`trace_id` parameter shape.** The brief asserts "LangSmith accepts external trace IDs via `LANGSMITH_TRACING_PROJECT` + `langsmith.trace_id` parameter." LangSmith Python SDK 0.8.x actually uses `langsmith.Client.create_run(trace_id=...)` and `@traceable(client=..., run_id=...)`. Plan uses the SDK 0.8.x shape; will name the convention in our wrapper so a future SDK bump is one-place fix.

5. **HTTP `X-Trace-Id` propagation.** Brief says "Anthropic supports, Twilio/Razorpay/Resend ignore." Implementation will add the header unconditionally to outbound HTTP from a small shared `request_with_trace(run_id, ...)` helper; no per-vendor branching. Out-of-band tracing on the LLM provider side is a nice-to-have, not a gate.

6. **Token / time budget.** Brief budget is 100K tokens. Estimate is 110K (plan 20K + impl 80K + tests 10K) — slightly over. Plan splits cleanly if Cowork wants 2 PRs: PR1 `feat/vt-observability-langsmith-core` (langsmith.py + pii.py + .env + pyproject + 4 tests + the decorator wiring on runner.py); PR2 `feat/vt-observability-langsmith-edges` (twilio_ingress.py wrap, agent wrap, Telegram footer, pipeline_log assertion, remaining 2 tests, PII doc). I lean single-PR — the scope is one logical seam — but flagging the option here so Cowork can pre-authorize a split if it prefers.

7. **`mypy --strict` on a new module.** Brief acceptance includes mypy strict pass. The `@traceable` decorator from langsmith returns `Any` under strict; I'll narrow with a `cast` at the decorator-application site to keep call-site types intact. Standard pattern.

8. **Graceful degradation test (#6) needs careful mock placement.** `langsmith.Client.create_run` raises — but `@traceable` swallows the error by default. The test will patch at the underlying `langsmith.client.LangSmithClient._tracing_thread_create_run` (or whichever low-level call surfaces) to actually raise in-band, then assert pipeline completes. If LangSmith SDK swallows by design, the test becomes "raise + assert no exception escapes the orchestrator" which is the same observable contract — graceful by SDK default still satisfies the brief.
