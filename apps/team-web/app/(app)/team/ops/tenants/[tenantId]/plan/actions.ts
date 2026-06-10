'use server'

/**
 * VT-370 Gap-6 — plan-edit server action.
 *
 * Gated by requireOpsOperator(); operator_id is derived from the SESSION claim — never a
 * client field (the orchestrator independently re-verifies body operator_id == JWT claim +
 * assignment, fail-closed). The patch is whitelisted to EDITABLE_FIELDS here too (defense
 * in depth — the seam enforces it authoritatively). CL-390: this action logs nothing —
 * violations come back scrubbed for RENDERING only.
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { vtrPlanEdit, type VtrPlanEditResult } from '@/lib/orchestrator-client'

/** Mirror of seams.EDITABLE_FIELDS — the only patch keys a VTR may send. */
const EDITABLE_FIELDS = new Set([
  'objective',
  'why',
  'month',
  'owner_action',
  'owner_action_hi',
  'owner_action_needed',
  'status',
  'owning_agent',
])

export async function editRoadmapItemAction(
  tenantId: string,
  itemId: string,
  patch: Record<string, unknown>,
  expectedPrevVersion: number,
): Promise<VtrPlanEditResult> {
  const operator = await requireOpsOperator()
  // Early assignment check (UX only — enforcement is the orchestrator's fail-closed gate).
  if (operator.assignedTenants !== null && !operator.assignedTenants.includes(tenantId)) {
    return { ok: false, newVersion: null, reason: 'forbidden', violations: [] }
  }
  const clean = Object.fromEntries(
    Object.entries(patch ?? {}).filter(([k]) => EDITABLE_FIELDS.has(k)),
  )
  if (Object.keys(clean).length === 0) {
    return { ok: false, newVersion: null, reason: 'empty_patch', violations: [] }
  }
  return vtrPlanEdit(operator.operatorId, tenantId, itemId, clean, expectedPrevVersion)
}
