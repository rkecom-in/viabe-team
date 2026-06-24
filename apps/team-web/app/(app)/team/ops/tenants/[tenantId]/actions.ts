'use server'

/**
 * VT-405 Part B — confirm-field server action.
 *
 * Gated by requireOpsOperator(); operator_id is derived from the SESSION claim — never a client
 * field (ops-actions-resolve-scope-serverside; the orchestrator independently re-verifies body
 * operator_id == JWT claim + assignment + the require_vtr_action gate, fail-closed). Per CL-441 a
 * VTR may confirm ANY discovered field (identity included) — so NO field whitelist here; we only
 * reject the reserved `_field_provenance` key (the seam rejects it authoritatively too). The field
 * VALUE is read server-side from the draft by the orchestrator — the client sends only the NAME.
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { vtrConfirmField, type VtrConfirmFieldResult } from '@/lib/orchestrator-client'

export async function confirmFieldAction(
  tenantId: string,
  field: string,
): Promise<VtrConfirmFieldResult> {
  const operator = await requireOpsOperator()
  // Early assignment check (UX only — enforcement is the orchestrator's fail-closed gate).
  if (operator.assignedTenants !== null && !operator.assignedTenants.includes(tenantId)) {
    return { ok: false, field: null, status: null, reason: 'forbidden' }
  }
  const clean = (field ?? '').trim()
  if (!clean || clean === '_field_provenance') {
    return { ok: false, field: null, status: null, reason: 'invalid_field' }
  }
  return vtrConfirmField(operator.operatorId, tenantId, clean)
}
