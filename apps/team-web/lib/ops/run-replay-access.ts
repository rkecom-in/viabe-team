/**
 * VT-412 — run-replay access + de-identification helpers (PR-D: open the ops
 * run-replay cluster to VTR operators with proper scoping + de-identification).
 *
 * The four run-replay pages (stream / stream-history / runs/[runId] /
 * runs/[runId]/debug) moved from requireFazal() to requireOpsOperator() +
 * per-tenant assignment scoping. The data path now splits by role:
 *
 *   - VTAdmin / Fazal (assignedTenants === null) → FULL service-role read
 *     (the existing fetchRunReplay / fetchHistoricalSteps), unchanged.
 *   - VTR (assignedTenants is a string[]) → a SCOPED + DE-IDENTIFIED read:
 *       * single-run replay reads through vtrRunTimeline → the
 *         orchestrator /timeline/{run_id} → the mig-132/134 vtr_step_timeline
 *         VIEW (assignment-scoped + de-identified + IDOR-safe by construction —
 *         decision_rationale / error / tool_calls are NOT in the view at all).
 *       * the server-side history read is scoped to the operator's assigned
 *         tenants AND de-identified by deIdentifyStepForVtr (below) — a
 *         conservative superset of the view's redaction (keys-only envelopes
 *         for ALL kinds; no decision_rationale / error / tool_calls).
 *
 * Extracted to its own dep-less module so the access decisions are unit-
 * falsifiable in vitest WITHOUT importing a server component (the pages pull
 * next/navigation + requireOpsOperator + the orchestrator client). The pages
 * import + use this; the test imports only this. Mirrors the VT-377
 * scope-tenants.ts idiom (no component harness invented).
 *
 * IDOR rule (VT-293/294, caught twice): the tenant a run/step belongs to is
 * ALWAYS resolved server-side from the run row; NEVER a client-supplied
 * tenantId. canReplayRun below takes the SERVER-RESOLVED tenantId.
 */

import { canAccessTenant } from '@/lib/ops/assignments'
import type { PipelineStepRow } from '@/lib/ops/data-access'

/** True iff this operator may replay a run belonging to `runTenantId` (resolved
 *  server-side from the run row — never a client field). VTAdmin (null) always;
 *  a VTR iff the run's tenant is in its active assigned set; fail-closed empty.
 *  Thin wrapper over canAccessTenant so the call-site reads as the intent. */
export function canReplayRun(
  assignedTenants: string[] | null,
  runTenantId: string | null,
): boolean {
  if (runTenantId === null) return false // unknown tenant ⇒ fail-closed (no replay)
  return canAccessTenant(assignedTenants, runTenantId)
}

/** True iff this operator reads through the FULL service-role path (VTAdmin /
 *  Fazal). A VTR (non-null assigned set) reads the de-identified path instead. */
export function hasFullReadAccess(assignedTenants: string[] | null): boolean {
  return assignedTenants === null
}

/** Reduce an arbitrary envelope to its top-level KEY NAMES only — what a step
 *  carried, never to-what. Mirrors the vtr_step_timeline default branch
 *  (keys-only for non-audited kinds); applied to ALL kinds here so this TS
 *  projection can never expose a value the SQL view would hide (a conservative
 *  superset — strictly more redacted, so it cannot drift OPEN from the view). */
export function envelopeKeysOnly(envelope: unknown): string[] | null {
  if (envelope === null || envelope === undefined) return null
  if (typeof envelope !== 'object' || Array.isArray(envelope)) return []
  return Object.keys(envelope as Record<string, unknown>)
}

/**
 * De-identify ONE pipeline_steps row for a VTR history read. Drops the three
 * PII-bearing columns the vtr_step_timeline view excludes by construction
 * (decision_rationale — agent think-text; error — stack/message; tool_calls —
 * arg payloads) and reduces both envelopes to key-lists. Numeric / enum / id
 * columns (seq, kind, status, model, tokens, cost, timings) pass through —
 * they carry no customer identity. The result is render-safe for a VTR.
 */
export function deIdentifyStepForVtr(step: PipelineStepRow): PipelineStepRow {
  return {
    ...step,
    decision_rationale: null,
    error: null,
    tool_calls: null,
    input_envelope: envelopeKeysOnly(step.input_envelope),
    output_envelope: envelopeKeysOnly(step.output_envelope),
  }
}

/**
 * Scope a client-requested tenant filter to what the operator may actually see.
 *   - VTAdmin (null): the client filter passes through verbatim (unscoped).
 *   - VTR: the EFFECTIVE filter is the intersection of the client's requested
 *     tenants with the assigned set; an empty/absent client filter defaults to
 *     the WHOLE assigned set (never "all tenants"). The result is NEVER empty-
 *     meaning-all — a VTR with no assignments yields [] which the caller must
 *     treat as "show nothing" (fail-closed), the opposite of an absent filter.
 *
 * Returns `{ tenantIds, denied }`: `denied` is true iff a VTR's effective
 * filter is empty (no assigned tenant in scope) — the caller returns no rows.
 * NEVER trust the client tenantIds directly (IDOR) — this is the server-side
 * narrowing the history route applies before the query.
 */
export function scopeHistoryTenantFilter(
  assignedTenants: string[] | null,
  requestedTenantIds: string[] | undefined,
): { tenantIds: string[] | undefined; denied: boolean } {
  if (assignedTenants === null) {
    // VTAdmin — unscoped; pass the client filter through (undefined = all).
    return { tenantIds: requestedTenantIds, denied: false }
  }
  const assigned = new Set(assignedTenants)
  if (assignedTenants.length === 0) {
    // VTR with no assignments — fail-closed, show nothing.
    return { tenantIds: [], denied: true }
  }
  if (!requestedTenantIds || requestedTenantIds.length === 0) {
    // No client filter ⇒ scope to the WHOLE assigned set (never "all").
    return { tenantIds: [...assignedTenants], denied: false }
  }
  // Intersection: only requested tenants that are actually assigned.
  const effective = requestedTenantIds.filter((t) => assigned.has(t))
  return { tenantIds: effective, denied: effective.length === 0 }
}
