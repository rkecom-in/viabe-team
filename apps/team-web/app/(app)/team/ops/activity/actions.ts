'use server'

/**
 * VT-293 — Activity server actions. Gate on requireOpsOperator (role + scoping), then the
 * authorized writer. escalateRun → escalations + ops_audit; flagRunControl → ops_audit
 * intent (actual orchestrator run-control is the DBOS-side follow-up).
 */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { escalateRun, flagRunControl } from '@/lib/ops/activity'

export async function escalateRunAction(runId: string) {
  const op = await requireOpsOperator()
  return escalateRun(op, runId) // tenant resolved server-side from the run (no IDOR)
}

export async function flagRunControlAction(runId: string, control: string) {
  const op = await requireOpsOperator()
  return flagRunControl(op, runId, control)
}

export async function fetchRunStepsAction(runId: string) {
  const op = await requireOpsOperator()
  const { fetchRunSteps } = await import('@/lib/ops/activity')
  return fetchRunSteps(op, runId)
}
