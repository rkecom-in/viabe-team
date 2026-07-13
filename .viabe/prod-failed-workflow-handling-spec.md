# Production Failed / Orphaned Workflow Handling — Containment + Diagnosis + VTR Reporting

**Requirement (Fazal 2026-07-10).** Origin: CC found orphaned DBOS `manager_task` workflows on DEV (harness
teardown deleted tenants mid-workflow → DBOS recovery re-ran the orphans after every redeploy → FK crashes +
dispatch churn that degraded measurement). Fazal: the SAME class of problem must be robustly handled in
PRODUCTION.

**Status:** ROSTERED requirement. Sequence = **prod-hardening / launch-gate (with or before VT-231 prod
cutover)**. NOT current Phase-1.1 scope — CC's active lever (reactive trust floor) is unchanged. Prod is not live
yet (VT-231 pending), so this is a build-before-real-prod item, not a live fire. VT-ID allocated when the row is
picked up.

## The requirement (Fazal's words, made precise)
When production gets an orphaned or failed workflow:
1. **REPORT it** — it must not fail silently.
2. **ACT on it**, in this order:
   - **(a) CONTAIN first — prevent re-run in ANY case.** Archive / mark disabled / quarantine — whatever the
     mechanism — so DBOS recovery (or any retry) does NOT keep re-running a broken/orphaned workflow. Fail-safe
     default: an orphaned/failed EFFECTFUL workflow is NOT auto-re-run.
   - **(b) DIAGNOSE (a SEPARATE process).** A distinct process analyzes failed workflows — what failed, why, which
     tenant, which step, and what EFFECT-STATE (did any send/spend partially happen?) — and produces findings.
   - **(c) SURFACE on the VTR console** with those findings, so the VTR human can take the necessary action
     (retry / cancel / escalate / manual fix).

## The safety nuance (Cowork flag — this is the hard part)
On EFFECTFUL / send paths, a workflow that PARTIALLY completed then failed (e.g. sent to some customers, crashed
before the rest) must NOT be:
- blindly RE-RUN → double-send to real customers (the exact trust-breaker we spent this cycle hardening against), NOR
- silently DISABLED → a half-done task lost; some customers messaged, others not, owner unaware.
So containment = **stop auto-re-run by default**; the DIAGNOSIS determines the effect-state (what already happened)
so the VTR (or a safe deterministic policy) resolves it correctly. An idempotency / effect-state ledger is the
enabler — you cannot safely act on a failed send workflow without knowing exactly what it already did.

## Remediation model (Fazal 2026-07-10 — Fazal will define the FULL process)
Resolution of the partial-failure trap:
1. **DISABLE the original workflow** — explicit purpose: prevent a re-run that would REPEAT already-executed
   (duplicate) actions.
2. **DIAGNOSE** — the diagnosis process analyzes: what happened, what caused it, WHAT GOT SENT (already executed),
   WHAT IS PENDING (not yet executed).
3. **INJECT a new PARTIAL sub-process** — a fresh sub-process that completes ONLY the PENDING remainder (the
   un-sent portion), never re-doing what already went out. Partial-by-design.
Net: the original is made inert (no duplicates), and the remainder is finished cleanly via a scoped sub-process.
**Fazal is defining the entire process** — this captures the shape only. Taken up LATER with other ops requirements.

## Build components
1. **Containment:** intercept orphaned/failed workflows and quarantine them (no auto-recovery re-run), especially
   effectful ones. Extends VT-420 (crash-safety / re-send window) + send idempotency.
2. **Detection + report:** orphaned/failed workflows detected + logged, never silent.
3. **Diagnosis process (separate):** analyzes failed workflows → structured findings (cause, tenant, step,
   effect-state, recommended action).
4. **VTR-console surfacing:** findings on the VTR console (extends VT-514 audit / VT-515 debug / VT-516 trace
   viewer) with human-action affordances (retry / cancel / escalate).

## Boundaries
- Human-in-the-loop: the VTR takes the action; the system CONTAINS + DIAGNOSES + REPORTS — it does not silently
  auto-resolve an effectful failure.
- Effect-boundary / Pillar-7: any re-run that would RE-SEND needs the same approval + idempotency guarantees;
  never a silent double-send.
- Prod infra/DB changes are Fazal-authorized (CL-431).

## References (do NOT rebuild from scratch)
- VT-420 — restart-mid-send re-send window / crash-safety (the send-idempotency substrate).
- VT-514 / VT-515 / VT-516 — the VTR console audit/debug/trace viewer to EXTEND.
- CC's dev #53 orphan-teardown is HARNESS-specific (cancel workflows before tenant delete); the PROD mechanism is a
  DBOS recovery-policy + diagnosis + VTR reporting — a DIFFERENT mechanism, do not conflate.
