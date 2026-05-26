---
reviewer: cowork
verdict: APPROVED-with-conditions
ts: 2026-05-26T03:08:00+05:30
plan_sha: (queue/VT-104/plan.md)
---

# Review — VT-104 plan

**APPROVED with 3 conditions** + both open questions answered. Plan is the most carefully scoped I've reviewed in this run — CC named the regression posture explicitly, internalised the "too clean to be true" warning, and proposed sensible scope-trims for two real gaps (no `customers` table; bank-account regex collision).

## Answers to your 2 plan-ready questions

**Q1 (customers table) — Ship with `name_registry: Callable | None` fallback.** Recommend NOT creating `customers` table in this row. Schema impacts L1/L2 KG + customer-state machine + DSR routines. Out of scope for a PII redactor. Will file a follow-up VT row separately (sequence the row when L1/L2 work or DSR purge actually needs it; not before).

**Q2 (token format split) — APPROVED.** VT-101's existing named-key tokens (`phone_tok_HEX`, `body_tok_HEX`, `<redacted:customer_name:len=N>`) MUST stay byte-identical. New pattern types use brief's `<type:redacted>` / `<type:hash:HEX>` format. Pillar 8 is "one redactor"; the token format is internal API. Changing it would invalidate audit artifacts + break downstream pattern-matchers (which `observability/query.py` paths may rely on).

## Conditions (must address before pr-ready)

### Condition 1 — Bank-account narrowing is intentional; document it loudly

Risk #2's resolution is sound: narrowing bank-account redaction to NAMED-KEY ONLY (`{"bank_account", "account_number", "acct_no"}`) avoids regex collision with phone / Aadhaar / CC. But this partially defeatures brief §1 fifth bullet. Document explicitly in TWO places:

1. **Module docstring of `pii_redactor.py`:** prominent note that bank-account redaction is KEY-DRIVEN ONLY (regex variant intentionally skipped to avoid false positives with Aadhaar / phone / CC). Future row can add structured bank-account format detection when there's a real source of bank account values.
2. **Sprint file's Out of scope addendum** (append at end of `.viabe/sprint/VT-104.md` body): "Pure-digit bank-account regex detection deferred (collision risk with 12-digit Aadhaar / 13-19-digit CC / 10-digit phone)."

This is a Type-2 narrowing of the brief — documenting both places makes it explicit rather than a quiet decision.

### Condition 2 — Regression assertions MUST re-run VT-101 + VT-102 canaries locally, paste outputs in pre-merge-result

Plan's risk #7 ("too clean to be true") says you'll re-verify VT-101 + VT-102 canaries. Make this explicit in `pre-merge-result`:

- Run `vt101_langsmith.py` locally post-consolidation → paste FULL stdout (or tail) in the supplement signal
- Run `vt102_pipeline_log.py` locally post-consolidation → paste FULL stdout (or tail) in the supplement signal
- Both must show 7/7 PASS and 7/7 PASS respectively WITH byte-identical assertion output to their prior runs

This is the regression evidence. The canary's Group A assertions #1 + #2 cover the LangSmith + pipeline_log paths inline, but re-running the prior canaries end-to-end is a stronger check. Don't skip.

### Condition 3 — Reasoning trace integration is forward-pointing; ship docstring discipline

Risk #8: reasoning trace functions ship callable but no live agent SDK invokes them yet. Acceptable as ship-thin. But the `reasoning_trace.py` module docstring MUST state:

> *"Forward-pointing module: these functions are callable but no production code path currently invokes them. The VT-4 agent SDK integration that wires the call sites is a separate VT row (TBD). DO NOT modify the function signatures without updating the future-PR brief, since those signatures are the contract that agent-SDK PR will integrate against."*

Keeps future-CC honest about not shifting the contract without a paired plan.

## Out of scope (Cowork concurs — do NOT scope-creep)

- Creating `customers` table migration (separate future row)
- Wiring reasoning_trace into VT-4 agent SDK (separate future row)
- Changing VT-101's token format (regression preservation)
- ML/fuzzy customer name detection (Phase 2 per brief)
- Pure-digit bank-account regex (per Condition 1)

## Cowork follow-up after VT-104 merges

I'll file:
- **VT-170 candidate** (next allocator number) — "customers table + tenant customer registry" — scoped large enough to cover L1/L2 KG integration + DSR routine integration; Backlog priority until L1/L2 or DSR needs it.

Don't include this in your VT-104 PR. Cowork-side row creation post-merge, following the VT-169 pattern from VT-102.

## Single-PR strong preference

175K / 180K ceiling. If you cross 180K mid-flight, do the split per risk #6: PR-A canonical redactor + tests + observability/pii.py rewrite + canary; PR-B reasoning_trace + replay + reasoning_trace tests. Surface via `plan-updated` — don't push past silently.

## Rule #15 audit standard

`pre-merge-result` MUST include verbatim:
- Total canary wall-clock (`time` output)
- Resolved Anthropic + Supabase hosts at PREFLIGHT (credentials stripped)
- Per-assertion observed values for all 10 assertions
- **PLUS the re-run outputs of VT-101 canary + VT-102 canary post-consolidation** per Condition 2
- Captured Anthropic cost (token counts × `model_pricing.yaml` rate; assert < ₹1)
- Full canary stdout tail ~150 lines; full log at `/tmp/vt104-canary-evidence.log`

Summary-only will be bounced. Standard set by VT-102; tightened for VT-104 with the regression re-runs.

## Authority

Flip `.viabe/queue/VT-104/status` from `review` → `implementing` and proceed.

Pillar 7: merge requires Fazal `type: task` with `authorized_by: fazal`.

Go.
