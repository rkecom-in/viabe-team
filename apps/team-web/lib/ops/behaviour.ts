/**
 * VT-294 — Decision Audit: measure VTR decision quality from ops_audit (CL-426).
 *
 * Reads the ops_audit trail (VT-292 substrate). A VTR sees their OWN decision activity; a
 * VTAdmin sees all operators' (to measure VTRs). Metrics = action counts over a window +
 * recent decisions. The "train" control records corrective feedback (an ops_audit row,
 * action='train') — the structured feedback that future decision-quality scoring builds on.
 * Deny-all table → serverSecretClient; scoping app-side. No PII (ops_audit carries none).
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'

type Client = { from: (t: string) => any }

interface ActingOperator {
  operatorId: string
  role: OperatorRole
  assignedTenants: string[] | null
}

export interface DecisionMetrics {
  scope: 'own' | 'all'
  total: number
  byAction: Record<string, number>
}

export interface DecisionRow {
  id: string
  operator_id: string
  action: string
  target_kind: string
  target_id: string | null
  created_at: string | null
}

function _since(days: number): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - days)
  return d.toISOString()
}

export async function fetchDecisionMetrics(
  op: ActingOperator,
  client: Client = serverSecretClient(),
  windowDays = 30,
): Promise<DecisionMetrics> {
  const ownOnly = op.role !== OperatorRole.VTADMIN // VTR → own decisions only
  let q = client.from('ops_audit').select('action').gte('created_at', _since(windowDays))
  if (ownOnly) q = q.eq('operator_id', op.operatorId)
  const { data } = await q
  const byAction: Record<string, number> = {}
  for (const r of (data ?? []) as { action: string }[]) {
    byAction[r.action] = (byAction[r.action] ?? 0) + 1
  }
  const total = Object.values(byAction).reduce((a, b) => a + b, 0)
  return { scope: ownOnly ? 'own' : 'all', total, byAction }
}

export async function fetchRecentDecisions(
  op: ActingOperator,
  client: Client = serverSecretClient(),
  limit = 20,
): Promise<DecisionRow[]> {
  const ownOnly = op.role !== OperatorRole.VTADMIN
  let q = client
    .from('ops_audit')
    .select('id, operator_id, action, target_kind, target_id, created_at')
    .order('created_at', { ascending: false })
    .limit(limit)
  if (ownOnly) q = q.eq('operator_id', op.operatorId)
  const { data } = await q
  return (data ?? []).map((r: any) => ({
    id: String(r.id),
    operator_id: String(r.operator_id),
    action: r.action,
    target_kind: r.target_kind,
    target_id: r.target_id,
    created_at: r.created_at,
  }))
}

/** Record corrective feedback on a decision (the "train" control). Writes an ops_audit
 *  row (action='train') — the structured-feedback substrate decision-quality scoring uses.
 *  VTAdmin can train any operator's decision; a VTR only their own.
 *
 *  IDOR-hardened (security review): for a non-admin, the decision's TRUE owner is RESOLVED
 *  from ops_audit by targetDecisionId and compared to op.operatorId — never a client-supplied
 *  operator field. No client scoping arg. Fail-closed. */
export async function recordTraining(
  op: ActingOperator,
  targetDecisionId: string,
  note: string,
  client: Client = serverSecretClient(),
): Promise<{ ok: boolean; reason?: string }> {
  if (op.role !== OperatorRole.VTADMIN) {
    // resolve the decision's real owner; a VTR may only train their OWN decisions.
    const { data } = await client
      .from('ops_audit')
      .select('operator_id')
      .eq('id', targetDecisionId)
      .limit(1)
    const dec = (data ?? [])[0] as { operator_id: string } | undefined
    if (!dec) return { ok: false, reason: 'decision not found' }
    if (String(dec.operator_id) !== op.operatorId) {
      return { ok: false, reason: 'VTR can only train own decisions' }
    }
  }
  await client.from('ops_audit').insert({
    operator_id: op.operatorId,
    tenant_id: null,
    action: 'train',
    target_kind: 'decision',
    target_id: targetDecisionId,
    detail: note,
  })
  return { ok: true }
}
