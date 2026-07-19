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
import {
  vtrAgentDirective,
  vtrConfirmField,
  vtrOwnershipDecision,
  type VtrAgentDirectiveResult,
  type VtrConfirmFieldResult,
  type VtrOwnershipDecisionResult,
} from '@/lib/orchestrator-client'

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

/**
 * VT-517 — record a human ownership decision (verified | rejected). Same gating shape as
 * confirmFieldAction: requireOpsOperator() + an early assignment guard (UX only); operator_id is
 * derived from the SESSION claim, never the client (ops-actions-resolve-scope-serverside). The
 * orchestrator independently re-verifies body operator_id == JWT claim + assignment, fail-closed,
 * and writes the tenants row + ops_audit + tm_audit in one transaction.
 */
export async function verifyOwnershipAction(
  tenantId: string,
  decision: 'verified' | 'rejected',
  note: string,
  evidence: string,
): Promise<VtrOwnershipDecisionResult> {
  const operator = await requireOpsOperator()
  if (operator.assignedTenants !== null && !operator.assignedTenants.includes(tenantId)) {
    return { ok: false, decision: null, ownershipVerified: false, reason: 'forbidden' }
  }
  return vtrOwnershipDecision(operator.operatorId, tenantId, decision, note, evidence)
}

/**
 * VT-556 — a VTR ingests a strategy/behavioural directive the Team Manager picks up next run.
 * Same gating shape as the actions above: requireOpsOperator() + an early assignment guard (UX
 * only); operator_id is derived from the SESSION claim, never the client. The orchestrator
 * independently re-verifies operator_id == JWT claim + assignment (require_vtr_action, fail-closed)
 * and writes agent_memory + ops_audit + tm_audit.
 */
export async function ingestDirectiveAction(
  tenantId: string,
  memoryKey: string,
  content: string,
  directiveKind: 'strategy' | 'behavioural',
): Promise<VtrAgentDirectiveResult> {
  const operator = await requireOpsOperator()
  if (operator.assignedTenants !== null && !operator.assignedTenants.includes(tenantId)) {
    return { ok: false, version: null, memoryKey: null, reason: 'forbidden', violations: [] }
  }
  const key = (memoryKey ?? '').trim()
  const body = (content ?? '').trim()
  if (!key || !body) {
    return { ok: false, version: null, memoryKey: null, reason: 'invalid', violations: [] }
  }
  return vtrAgentDirective(operator.operatorId, tenantId, key, body, directiveKind)
}
