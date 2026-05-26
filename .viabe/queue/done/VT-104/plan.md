---
task: VT-104
author: claudecode
ts: 2026-05-26T03:12:00+05:30
estimated_tokens: 175000
estimated_minutes: 145
---

## Approach

Ship the canonical `apps/team-orchestrator/src/orchestrator/privacy/pii_redactor.py::redact()` as the **single source of truth** for redaction. The existing `observability/pii.py::redact_for_langsmith` / `redact_for_log` collapse into **thin delegating wrappers** preserving their exact current output (so VT-101 and VT-102 canaries + the 4 PR-#56/#57/#58 passing canary assertions stay byte-identical). New pattern types (PAN, Aadhaar, IFSC, GST, Bank, CC, long-body, customer-name-registry) extend the wrapper output additively — no existing token format changes.

This is the **safest regression posture**: VT-101's wire payload uses `phone_tok_HEX` / `body_tok_HEX` / `<redacted:customer_name:len=N>` tokens that LangSmith traces and `pipeline_log` JSONB rows already carry. Changing the format would invalidate prior audit artifacts + the read paths in `observability/query.py` that downstream tools may pattern-match on. Brief's example formats (`<phone:hash:abc123>`) treat as illustrative — Pillar 8 is "one redactor", not "one token format string"; the latter is internal API.

Customer-name tokenization: **no `customers` table exists** (migration 000-022 audit; surfaced as plan risk #1). Ship the registry lookup as a **caller-provided callable** (`name_registry: Callable[[str], bool] | None = None`); when `None`, the redactor degrades to "regex-only mode" + emits a debug-level breadcrumb so the gap is observable. The canary's Group C #8 wires a synthetic in-memory `set("Rajesh Kumar")` registry as the callable. When the real `customers` table ships (future VT row), one line wires `tenant_connection` + `SELECT name FROM customers WHERE tenant_id = ...` and the redactor immediately benefits — no API change.

Reasoning trace: thin module that emits `agent_reasoning_step` / `tool_call_args` / `tool_call_result` event types via VT-102's `log_event`. Extends `event_schemas.py` with these 3 entries. The full agent-SDK reasoning loop integration is deferred to a follow-up test row per brief §Out of canary scope; this module ships **callable**, ready for VT-4's agent SDK to invoke.

Replay script: prints the redacted timeline; uses `query_run(run_id)` from VT-102 directly.

## File changes

- **NEW `apps/team-orchestrator/src/orchestrator/privacy/__init__.py`** — package marker re-exporting `redact`.

- **NEW `apps/team-orchestrator/src/orchestrator/privacy/pii_redactor.py`** — canonical `redact(value, depth=0, max_depth=5, name_registry=None) -> Any`. Recursive walker over dict/list/tuple/str. Patterns (ordered to avoid collision):
  1. Customer/owner names (only when `name_registry` provided + key matches one of the PII keys OR exact-match scan inside body strings — registry-driven exact match only Phase 1).
  2. Phone (E.164 + 10-digit Indian) — hashes via `phone_token.hash_phone` for E.164-shaped tokens; inline `body_tok_`-style hash for unstructured strings (preserves VT-101 output byte-identical).
  3. Email — `[\w.+-]+@[\w.-]+\.\w+` → `<email:hash:HEX>` (NEW — not in VT-101's pii.py).
  4. PAN — `\b[A-Z]{5}[0-9]{4}[A-Z]\b` → `<pan:redacted>`.
  5. Aadhaar — `\b\d{4}\s?\d{4}\s?\d{4}\b` AFTER phone regex (10-digit phones already substituted). Returns `<aadhaar:redacted>`.
  6. IFSC — `\b[A-Z]{4}0[A-Z0-9]{6}\b` → `<ifsc:redacted>`.
  7. GSTIN — `\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b` → `<gst:redacted>`.
  8. Credit card — find 13-19 digit runs, validate Luhn, redact iff Luhn-valid → `<cc:redacted>`. Non-Luhn 16-digit runs (order IDs etc.) untouched.
  9. Long message body (>200 char) — hashes to `<body:hash:HEX>` (NEW format; not used by VT-101 which uses `body_tok_`-style for *named keys*; the long-body path applies only to inline raw strings that aren't a named PII key).
  10. Bank account 9-18 digits — INTENTIONALLY narrower than brief (`\b\d{9,18}\b` after all numeric-PII patterns above and only when not already matched). Surface as plan risk #2: brief says "bank account / IFSC / GST" — IFSC + GST are deterministic format strings; pure-digit bank accounts collide with phones/Aadhaar/CC unless we serialize regex order carefully. Risk-controlled by ordering + a `bank` key in `_PII_KEYS` (already-keyed values get the redacted marker without regex matching).

  Plus the **existing key-driven redaction** from `observability/pii.py::_PII_KEYS` (phone, customer_name, body, etc.) — preserved exactly so VT-101 / VT-102 / VT-103 outputs don't drift.

  `max_depth=5` per brief; depth-6+ returns `<redaction_truncated>` marker.
  Idempotent by construction: the output tokens (`phone_tok_`, `<phone:hash:`, `<email:hash:`, `<pan:redacted>`, `<aadhaar:redacted>`, `<ifsc:redacted>`, `<gst:redacted>`, `<cc:redacted>`, `<body:hash:`, `<customer_name>`, `<redacted:...>`) don't themselves match the patterns; second redaction is a no-op.

- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/pii.py`** — rewrite as thin delegation: `redact_for_langsmith(value, _depth=0) = redact(value, depth=_depth)` (so VT-101 / VT-102 / VT-103 keep their public surface). Keep the `redact_for_log` alias. The new `redact()` accepts the same input shapes; key-driven output keeps VT-101's format (`phone_tok_`, `body_tok_`, `<redacted:customer_name:len=N>`) byte-identical.

- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/__init__.py`** — re-export `redact` from `privacy` so observability call sites can use `redact()` directly.

- **NEW `apps/team-orchestrator/src/orchestrator/observability/reasoning_trace.py`** — `capture_agent_reasoning_step(run_id, tenant_id, *, step_name, content, metadata=None)`, `capture_tool_call_args(run_id, tenant_id, *, tool_name, args)`, `capture_tool_call_result(run_id, tenant_id, *, tool_name, ok, result, error=None)`. Each redacts payload via `redact()` then dispatches to `log_event(event_type=..., ..., payload=safe_payload)`. Event types `agent_reasoning_step`, `tool_call_args`, `tool_call_result` added to `event_schemas.py`.

- **MODIFY `apps/team-orchestrator/src/orchestrator/observability/event_schemas.py`** — add the 3 new event types with required + optional fields.

- **NEW `apps/team-orchestrator/scripts/replay_run.py`** — `python replay_run.py <run_id>` → calls `query_run(run_id)` + prints chronological timeline with event_type + component + payload summary. Service-role-only (uses `get_pool().connection()` directly). Top-line doc that this is ops-only Phase 1.

- **NEW `apps/team-orchestrator/tests/orchestrator/privacy/__init__.py`** + **`test_pii_redactor.py`** — 12 pure unit tests covering all 11 brief §4 bullets (phone E.164, 10-digit Indian phone, email, PAN, Aadhaar, IFSC, GST, CC valid Luhn, CC invalid Luhn, long body, customer-name registry match, customer-name unknown false-negative, recursive nested, max-depth truncation, idempotency).

- **NEW `apps/team-orchestrator/tests/orchestrator/observability/test_reasoning_trace.py`** — 4 tests per brief §5 (capture happy-path via monkeypatched `_do_insert_sync`; PII redaction at capture; replay timeline shape; cross-run isolation via run_id scope).

- **NEW `apps/team-orchestrator/canaries/vt104_pii_redactor.py`** — 10-assertion canary, structure mirroring VT-102/103. Group A regression (VT-101 LangSmith + VT-102 pipeline_log byte-identical), Group B 7 pattern types (phone, email, PAN/Aadhaar/IFSC/GST, CC Luhn, long body), Group C customer-name registry callable, Group D idempotency + recursion, Group E real Anthropic Haiku call with PII-stripped prompt.

## Test plan

- `pytest tests/orchestrator/privacy/ tests/orchestrator/observability/ -q` — full suite passes; new tests added; existing VT-101 / VT-102 tests remain green (regression).
- `ruff check apps/team-orchestrator` — clean.
- VT-101 canary (`vt101_langsmith.py`) re-run locally after pii.py rewrite — 6/6 PASS unchanged.
- VT-102 canary (`vt102_pipeline_log.py`) re-run locally — 7/7 PASS unchanged.
- VT-104 canary — 10/10 PASS against real Supabase dev pooler + real Anthropic. Verbatim audit artifact in `pre-merge-result`.

## Risks

1. **No `customers` table exists.** Migration audit confirms: brief §1 last bullet ("tokenize known customer names from the tenant's customers table") cannot resolve a SQL registry today. Plan ships `name_registry: Callable[[str], bool] | None` parameter; canary Group C wires a synthetic in-memory `{"Rajesh Kumar"}`. When a future VT row adds the `customers` table, one line wires the SQL lookup. **Surfacing as plan-ready question:** is this fallback acceptable, or does Cowork want VT-104 to also create migration `023_customers.sql`? My recommendation: defer the table to a dedicated future row (`customers` schema impacts L1/L2 knowledge graph + customer-state machine + DSR routines — way beyond a PII-redactor scope) and ship the callable now.

2. **Bank account 9-18-digit pattern collides with phone / Aadhaar.** Regex order: phone (10-digit Indian) → Aadhaar (12-digit) → CC (13-19 Luhn-valid). A raw 9-digit bank account string isn't reliably distinguishable from anything else. I'll narrow bank-account redaction to **named-key only** (key in `{"bank_account", "account_number", "acct_no"}`) and SKIP the regex variant entirely. Phone- + Aadhaar-shaped strings still get caught by their patterns. Document explicitly in the docstring.

3. **Token format inconsistency between brief and VT-101 output.** Brief uses `<phone:hash:abc123>` example; VT-101's output uses `phone_tok_HEX`. The redactor preserves VT-101's existing tokens so VT-101 + VT-102 canaries pass byte-identical (regression assertions #1 + #2). New pattern types get the brief's `<type:redacted>` format. **Decision baked into the canonical module:** key-driven redaction (named keys like `phone`, `body`, `customer_name`) uses VT-101's existing token style; pattern-driven redaction (PAN, Aadhaar, IFSC, GST, CC, raw-string long-body) uses the brief's `<type:redacted>` / `<type:hash:HEX>` style. Documented in module docstring.

4. **Idempotency requires output tokens to not match input patterns.** All output markers I emit (`phone_tok_`, `<phone:hash:`, `<email:hash:`, `<pan:redacted>`, `<aadhaar:redacted>`, `<ifsc:redacted>`, `<gst:redacted>`, `<cc:redacted>`, `<body:hash:`, `<customer_name>`, `<redacted:customer_name:len=N>`) are designed to NOT match any of the regex patterns. Tested in canary Group D #9 (real input → redact → byte-identical re-redact).

5. **Brief decay (same class as VT-101/102/103).** Path corrections (`apps/team/` → `apps/team-orchestrator/`), PR title `(VT-Observability-Cost)` → `(VT-104)`, merge target `main` not `dev`, retired reviewers skipped, VT-PrivacyArchitecture VT-8.9 dep is actually Backlog (`audit log` integration deferred — surfacing).

6. **Token budget tight.** 175K / 180K ceiling. Canary alone ~30K (10 assertions × 2 vendor surfaces). Will surface `plan-updated` if implementation crosses 180K mid-flight + propose split: PR-A canonical redactor + tests + observability/pii.py rewrite + canary; PR-B reasoning_trace + replay + reasoning_trace tests.

7. **"Too clean to be true" warning internalised.** If the canary passes 10/10 first run with zero changes to existing inline pii.py output, I will explicitly re-verify VT-101's canary still PASSES with byte-identical LangSmith trace JSON + VT-102's canary still PASSES with byte-identical pipeline_log JSONB. The regression assertions #1 + #2 are exactly this. If even one passes "too easily" I'll inspect the canary itself for fixture-side caching / silent fallthrough — the same trap that bit VT-101's initial canary (smoke version, not on-the-wire).

8. **Reasoning trace integration with VT-4 agent SDK is forward-pointing.** The trace functions are callable but no live agent invokes them yet. Brief §Out of canary scope acknowledges this. Documented in the module docstring; the future agent-SDK PR wires the call sites.

## Plan-ready questions for Cowork

- **Q1 (risk #1):** Ship redactor with `name_registry: Callable | None` fallback (recommend) OR create migration `023_customers.sql` in this row (large blast radius)?
- **Q2 (risk #3):** Token format split — VT-101 named-key tokens preserved + brief format only for new pattern types. Acceptable?
