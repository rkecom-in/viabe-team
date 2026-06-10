'use server'

/**
 * VT-370 Gap-6 — agent-correction server actions.
 *
 * Every action gates on requireOpsOperator() and forwards the SESSION-derived operator_id —
 * never a client field. Enforcement (JWT == body operator_id, assignment fail-closed, audit)
 * lives in the orchestrator's require_vtr_action; the early assignment check here is UX only.
 * vtr-batch-cancel / vtr-batch-drafts deliberately take NO tenant_id — the orchestrator derives
 * it from the batch row (VT-293/294 IDOR discipline). CL-390: actions log nothing; the drafts
 * payload (exception tier) is returned for rendering only.
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import {
  vtrAutonomyOverride,
  vtrBatchCancel,
  vtrBatchDrafts,
  type VtrBatchCancelResult,
  type VtrBatchDraft,
  type VtrOverrideAction,
  type VtrOverrideResult,
} from '@/lib/orchestrator-client'

const OVERRIDE_ACTIONS: ReadonlySet<string> = new Set(['freeze', 'unfreeze', 'demote', 'revoke_l3'])
const _REASON_MAX = 500

export async function autonomyOverrideAction(
  tenantId: string,
  agent: string,
  action: VtrOverrideAction,
  reason: string,
): Promise<VtrOverrideResult> {
  const operator = await requireOpsOperator()
  if (!OVERRIDE_ACTIONS.has(action)) {
    return { ok: false, state: null, batchesCancelled: 0, reason: 'invalid_action' }
  }
  if (operator.assignedTenants !== null && !operator.assignedTenants.includes(tenantId)) {
    return { ok: false, state: null, batchesCancelled: 0, reason: 'forbidden' }
  }
  return vtrAutonomyOverride(
    operator.operatorId,
    tenantId,
    agent,
    action,
    (reason ?? '').slice(0, _REASON_MAX),
  )
}

export async function cancelBatchAction(
  batchId: string,
  reason: string,
): Promise<VtrBatchCancelResult> {
  const operator = await requireOpsOperator()
  // NO tenant_id: derived server-side from the batch row (missing → 404; unassigned → 403).
  return vtrBatchCancel(operator.operatorId, batchId, (reason ?? '').slice(0, _REASON_MAX))
}

export async function batchDraftsAction(
  batchId: string,
): Promise<{ ok: boolean; drafts: VtrBatchDraft[]; reason: string }> {
  const operator = await requireOpsOperator()
  // Exception tier (Fazal=VTR#1): the orchestrator 403s anyone else and audits the reveal
  // in-txn before the read. Callers render a 403 gracefully.
  return vtrBatchDrafts(operator.operatorId, batchId)
}
