'use server'

/**
 * VT-713 — the infinite-scroll loader for the tenant activity flow. Auth is re-checked
 * INSIDE the action (server actions are network-callable — the page's guard is not enough):
 * requireOpsOperator + canAccessTenant, fail-closed, mirroring the data layer's own scoping.
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { canAccessTenant } from '@/lib/ops/assignments'
import { fetchFlowPage, type FlowPage } from '@/lib/ops/activity-flow'

export async function loadOlderFlow(tenantId: string, before: string | null): Promise<FlowPage> {
  let operator: Awaited<ReturnType<typeof requireOpsOperator>>
  try {
    operator = await requireOpsOperator()
  } catch {
    return { events: [], nextBefore: null }
  }
  if (!canAccessTenant(operator.assignedTenants, tenantId)) return { events: [], nextBefore: null }
  return fetchFlowPage(operator, tenantId, { before })
}
