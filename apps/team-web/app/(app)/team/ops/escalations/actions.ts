'use server'

/**
 * VT-292 — escalation server actions (resolve / ack / override).
 *
 * Each gates on requireOpsOperator (role + assignment scoping) then calls actOnEscalation,
 * which authorizes the tenant (VTR → assigned only), updates the row, and appends ops_audit.
 * The PII-reveal path stays on the VT-188 audited resolve-phone endpoint (separate).
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { actOnEscalation, type EscalationAction } from '@/lib/ops/escalations'

export async function escalationAction(
  escalationId: string,
  tenantId: string,
  action: EscalationAction,
  note?: string,
): Promise<{ ok: boolean; reason?: string }> {
  const operator = await requireOpsOperator()
  return actOnEscalation(operator, escalationId, tenantId, action, note ?? null)
}
