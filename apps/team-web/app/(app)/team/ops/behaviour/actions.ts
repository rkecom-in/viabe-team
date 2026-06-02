'use server'

/** VT-294 — Behaviour server action: record corrective feedback (train) on a decision. */

import { requireOpsOperator } from '@/lib/auth/require-ops-operator'
import { recordTraining } from '@/lib/ops/behaviour'

export async function trainAction(decisionId: string, note: string) {
  const op = await requireOpsOperator()
  // owner resolved server-side from the decision (no client scoping field — no IDOR).
  return recordTraining(op, decisionId, note)
}
