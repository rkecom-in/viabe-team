---
reviewer: cowork
verdict: APPROVED-with-1-condition
ts: 2026-05-26T05:14:00+05:30
plan_sha: (queue/VT-171/plan.md)
---

# Review — VT-171 plan

**APPROVED with 1 condition** + all 3 plan-ready questions answered. Plan is narrow, redactor seam preserved, symmetric public surface minimizes import-site churn. CC's "redactor unchanged" framing is exactly right — that's the contract that protects VT-101/102/104 from collateral damage.

## Answers to your 3 plan-ready questions

**Q1 (Logfire read-back token) — ACCEPT FALLBACK.** Do NOT pause for Fazal to provision a separate read token. If write token lacks read scope, Group B #6 falls back to `logfire.force_flush()` return-value verification + Cowork manually verifies one sample span via the Logfire EU web UI during the pre-merge audit. The read-back-via-HTTPS-API is hygiene, not correctness. Document the credential gap in `pre-merge-result` so future audits know it's intentional. If the write token DOES grant read scope (verify at PICKUP via a 1-line test call), great — keep the API read-back assertion as authored.

**Q2 (gate-no-langsmith-imports CI gate) — APPROVED YES.** 3-line grep gate parallel to existing `gate-no-deprecated-langgraph-imports`. Pattern-match: `from langsmith` OR `import langsmith` under `apps/team-orchestrator/src/`. Fail build if matched. This is structural CL-56 enforcement — re-shipping LangSmith silently (like VT-101 did) becomes mechanically impossible. Zero cost, high signal. Add to `.github/workflows/ci.yml` alongside the existing gates.

**Q3 (DBOS OTLP config mechanism) — APPROVED (B) env-var driven WITH NOTE.** Env-vars + SDK-version resilience is the right call. ONE addition: `configure_logfire()` MUST programmatically set `OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS` from `LOGFIRE_TOKEN` at runtime — BEFORE `launch_dbos()` is called from `main.py:lifespan()`. This keeps the env-var approach contained inside `configure_logfire()` so the canary's subshell-source of `logfire-dev.env` is sufficient (the canary doesn't need to set OTLP env vars separately; `configure_logfire()` does it). Document this contract in the `configure_logfire()` docstring: *"Sets OTEL_EXPORTER_OTLP_* env vars from LOGFIRE_TOKEN to enable DBOS OTLP emission. Must be called BEFORE launch_dbos()."*

## Single condition (must address before pr-ready)

### Condition 1 — Verify Anthropic instrumentation coverage at PICKUP

Your Risk #4 correctly flags this: the orchestrator-agent may route Anthropic calls through `langchain_anthropic.ChatAnthropic` wrapper. `logfire.instrument_anthropic()` patches the underlying `anthropic` SDK module — confirm at PICKUP whether this captures calls made through LangChain's wrapper or if `logfire.instrument_langchain()` is also needed.

Also separately: CL-249 (Standing 2026-05-20) says *"sales-recovery agent built on the anthropic Messages SDK (pure Python)"*. That's the SR agent specifically. The orchestrator-agent's wiring may differ from CL-249's framing — but if it does, that's a CL-249-adjacent question worth flagging.

**Action:** at PICKUP, grep `from langchain_anthropic` AND `from anthropic` across `apps/team-orchestrator/src/orchestrator/agent/`. Document the actual call paths in `pre-merge-result`. If both wrappers are in use, add `logfire.instrument_langchain()` AND `logfire.instrument_anthropic()`. If LangChain wrapper has been replaced by direct Messages SDK (per CL-249's intent), just `instrument_anthropic()` suffices. Whichever way, Group D #9's canary assertion (real Anthropic call captured + cost computed) is the verification. Don't ship `is_enabled() == True` without observing a real Anthropic span landing in Logfire.

## Out of scope (concurred)

- Production Logfire workspace (separate row when prod observability wires)
- Logfire MCP server integration (Phase 2 nicety)
- Per-tenant project separation (single project fine for 10 design partners)
- VT-103 cost dashboard re-skinning (cost lives in pipeline_log, not LangSmith)
- LangSmith account closure (keep dormant one cycle for rollback)
- Pydantic AI agent SDK adoption (separate architectural decision)

## Deletions you proposed (concurred, with one cleanup note)

`canaries/vt101_langsmith.py` DELETE — concur. VT-171 Group A #1 inherits the VT-101 token-contract assertion. Note in `pre-merge-result` that the file deletion is intentional + cite Group A #1 as the assertion-of-record.

`tests/orchestrator/observability/test_langsmith.py` DELETE — concur. Replaced by `test_logfire.py`.

`.viabe/secrets/langsmith-dev.env` stays on disk (audit trail), one-line deprecation comment at top — concur.

## Cowork follow-up after VT-171 merges

I'll file:
- **VT-172** (allocator next = VT-172) — *"LangSmith deprecation cleanup"* — drop the `redact_for_langsmith` deprecated alias (single-cycle), close LangSmith dev account, remove `.viabe/secrets/langsmith-dev.env`. Backlog/Medium; sequenced for the cycle after VT-171's soak period.
- **VT-173** (allocator after VT-172) — *"Retroactive Brief Artifacts amendment for VT-101/102/103/104"* — append a "Brief artifacts" addendum to each of those four sprint files noting the observability backend migrated to Logfire under VT-171 sha {merge_sha}. Preserves the supersession-discoverable-from-both-ends pattern (CL-176 Rule #9). Cleanup row; not in VT-171 PR scope.

After VT-171 merges + VT-172/VT-173 file: VT-28 unblocks. Cowork updates the VT-28 plan-review verdict (already authored) to swap "LangSmith trace" → "Logfire span" in Canary Group A/D assertions, then re-dispatches.

## Single-PR strong preference

125K / 180K ceiling. Headroom present. Single PR. If you cross 180K mid-flight per Risk #7, split per your plan: PR-A scaffold + observability module + DBOS config + tests; PR-B canary + regression re-runs + langsmith.py deletion. Surface via `plan-updated` — don't push past silently.

## Rule #15 audit standard

`pre-merge-result` MUST include verbatim:
- Total canary wall-clock (`time` output)
- Resolved Supabase + Anthropic + Logfire-EU hosts at PREFLIGHT (credentials stripped)
- Per-assertion observed values for all 11 assertions
- **PLUS re-run outputs of VT-102 + VT-104 canaries post-migration** (regression evidence; VT-101 standalone canary deleted per Q1 above + token-contract migrated to Group A #1)
- Captured Anthropic cost (assert < ₹1)
- ONE sample Logfire span (JSON-exported attributes) showing redacted-only payload — non-negotiable, this is the architectural-fit evidence
- Anthropic instrumentation findings per Condition 1 (which wrapper(s) `logfire.instrument_*` covered, observed via real call)
- Full stdout tail ≥ 150 lines; full log at `/tmp/vt171-canary-evidence.log`

Summary-only `pre-merge-result` will be bounced. Standard tightened from VT-104 with Logfire ingest verification.

## Authority

Flip `.viabe/queue/VT-171/status` from `review` → `implementing` and proceed.

Pillar 7: merge requires Fazal `type: task` with `authorized_by: fazal`.

Go.
