'use server'

/**
 * VT-376 (Phase C) — run-control MUTATION server actions.
 *
 * Every action gates on requireOpsOperator() and forwards the SESSION-derived operatorId —
 * NEVER a client field (the orchestrator independently re-verifies body operator_id == JWT
 * claim + the assignment gate, fail-closed, and audits BEFORE the mutation). The early
 * assignment check here is UX only — enforcement is the orchestrator's require_vtr_action.
 *
 * Scoping discipline (VT-293/294 IDOR):
 *   - pause/release/override(next-run) are tenant-scoped → the early assignment check applies.
 *   - cancel-override / rerun / override(row-targeted) are ROW-targeted → the orchestrator
 *     derives the tenant from the row; the UI sends only the id, and this layer does NOT
 *     pre-check a client tenant against the assignment set (it can't know the row's tenant).
 *
 * CL-390: these actions log nothing; scrubbed 4xx detail comes back for RENDERING only.
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import {
  vtrRcCancelOverride,
  vtrRcOverride,
  vtrRcPause,
  vtrRcRelease,
  vtrRcRerun,
  vtrRunTimeline,
  type RcCancelOverrideResult,
  type RcOverrideResult,
  type RcPauseResult,
  type RcReleaseResult,
  type RcRerunResult,
} from '@/lib/orchestrator-client'

/** UX-only early assignment check for tenant-scoped actions (server gate is authoritative). */
function _assigned(
  operator: Awaited<ReturnType<typeof requireOpsOperator>>,
  tenantId: string,
): boolean {
  return operator.assignedTenants === null || operator.assignedTenants.includes(tenantId)
}

export async function pauseAction(
  tenantId: string,
  workflowKind: string,
  reason: string,
): Promise<RcPauseResult> {
  const operator = await requireOpsOperator()
  if (!_assigned(operator, tenantId)) {
    return { ok: false, controlId: null, reason: 'forbidden' }
  }
  return vtrRcPause(operator.operatorId, tenantId, workflowKind, reason ?? '')
}

export async function releaseAction(
  tenantId: string,
  workflowKind: string,
): Promise<RcReleaseResult> {
  const operator = await requireOpsOperator()
  if (!_assigned(operator, tenantId)) {
    return { ok: false, controlId: null, reason: 'forbidden' }
  }
  return vtrRcRelease(operator.operatorId, tenantId, workflowKind)
}

export async function overrideAction(args: {
  tenantId: string
  workflowKind: string
  stepName: string
  /** set = row-targeted (tenant derived from the run); omitted = next-run (tenant-scoped). */
  workflowId?: string | null
  pinnedInput?: Record<string, unknown> | null
  pinnedOutput?: Record<string, unknown> | null
  reason?: string
  /** next-run pins only — UI defaults 7d; the server requires a future value when workflowId is null. */
  expiresAt?: string | null
}): Promise<RcOverrideResult> {
  const operator = await requireOpsOperator()
  // Tenant-scoped ONLY for the next-run path (no workflowId). Row-targeted overrides derive the
  // tenant from the run server-side — we cannot (and must not) pre-check a client tenant there.
  if (!args.workflowId && !_assigned(operator, args.tenantId)) {
    return { ok: false, overrideId: null, expiresAt: null, reason: 'forbidden', detail: [] }
  }
  return vtrRcOverride(operator.operatorId, args)
}

export async function cancelOverrideAction(
  overrideId: string,
): Promise<RcCancelOverrideResult> {
  const operator = await requireOpsOperator()
  // ROW-targeted: NO tenant_id — the orchestrator derives it from the override row (VT-293/294).
  return vtrRcCancelOverride(operator.operatorId, overrideId)
}

export async function rerunAction(
  sourceRunId: string,
  fromStep: string,
  overrides: Record<string, unknown>[] = [],
): Promise<RcRerunResult> {
  const operator = await requireOpsOperator()
  // ROW-targeted: NO tenant_id — derived from the source run row (VT-293/294).
  return vtrRcRerun(operator.operatorId, sourceRunId, fromStep, overrides)
}

/**
 * PRE-FLIGHT (plan §a3): re-fetch the run's live open-approval state immediately before a rerun
 * confirm. Returns ONLY a boolean (never the approval's contents). The server 409/422 on the
 * actual /rerun remains the authority — this is UI sugar so the operator sees "owner approval
 * pending — rerun will refuse" and a disabled submit instead of submitting into a guaranteed
 * refusal. Fail-SAFE: any read failure ⇒ openApproval=true (warn + block) rather than imply
 * "clear to rerun".
 */
export async function rerunPreflightAction(
  runId: string,
): Promise<{ openApproval: boolean; ok: boolean }> {
  const operator = await requireOpsOperator()
  const t = await vtrRunTimeline(operator.operatorId, runId)
  if (!t.ok) return { openApproval: true, ok: false } // fail-safe: warn + block on a degraded read
  return { openApproval: t.openApproval, ok: true }
}
