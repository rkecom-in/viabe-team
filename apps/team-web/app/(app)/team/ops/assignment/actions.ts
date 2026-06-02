'use server'

/**
 * VT-295 — assignment server actions (VTAdmin: assign / unassign businesses↔VTRs).
 *
 * Each gates on requireOpsOperator (role resolved server-side) then calls the
 * assignment-admin lib, which re-checks VTAdmin fail-closed, validates targets, resolves
 * the unassign target from the row id (IDOR rule), and appends ops_audit (Pillar 7).
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import {
  assignBusiness,
  unassignBusiness,
  type AssignmentResult,
} from '@/lib/ops/assignment-admin'

export async function assignAction(
  tenantId: string,
  operatorId: string,
  note?: string,
): Promise<AssignmentResult> {
  const operator = await requireOpsOperator()
  return assignBusiness(operator, tenantId, operatorId, note ?? null)
}

export async function unassignAction(
  assignmentId: string,
  note?: string,
): Promise<AssignmentResult> {
  const operator = await requireOpsOperator()
  return unassignBusiness(operator, assignmentId, note ?? null)
}
