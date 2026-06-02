/**
 * VT-292 — Escalations data-access + actions (Ops Console V2).
 *
 * The canonical escalation queue (migration 073). Reads are scoped to the operator's
 * assigned tenants (VTR → assigned, fail-closed; VTAdmin → all) + de-identified for VTR
 * (CL-426). Actions (resolve / ack / override) UPDATE the row AND append an ops_audit row
 * (migration 074) — the VT-188 privacy_audit_log stays for PII reveals only (answer #3).
 * Deny-all tables → serverSecretClient (service-role, RLS-bypassing); scoping is app-side.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'
import { maskForVtr, type MaskedOpsRow, type OpsRow } from '@/lib/ops/de-identify'

type Client = { from: (t: string) => any }

interface OpsOperatorLike {
  operatorId: string
  role: OperatorRole
  assignedTenants: string[] | null
}

export type EscalationAction = 'resolve' | 'ack' | 'override'

export async function fetchEscalations(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
  limit = 50,
): Promise<MaskedOpsRow[]> {
  const { assignedTenants } = operator
  if (assignedTenants !== null && assignedTenants.length === 0) return [] // fail-closed
  let q = client
    .from('escalations')
    .select('id, tenant_id, kind, severity, status, opened_at')
    .neq('status', 'resolved')
    .order('opened_at', { ascending: false })
    .limit(limit)
  if (assignedTenants !== null) q = q.in('tenant_id', assignedTenants)
  const { data } = await q
  const rows: OpsRow[] = (data ?? []).map((r: any) => ({
    id: String(r.id),
    tenant_id: String(r.tenant_id),
    kind: r.kind,
    severity: r.severity,
    time: r.opened_at,
    status: r.status,
  }))
  // VTR view masked; VTAdmin sees the same operational fields (escalations carry no PII
  // columns — masking is uniform to hold the contract as the source grows).
  return rows.map(maskForVtr)
}

/** Apply an escalation action: update status (resolve/ack) + append ops_audit. Returns the
 *  audited action. `override` resolves with an override marker. Fail-closed: a VTR may only
 *  act on an assigned tenant. */
export async function actOnEscalation(
  operator: OpsOperatorLike,
  escalationId: string,
  tenantId: string,
  action: EscalationAction,
  note: string | null = null,
  client: Client = serverSecretClient(),
): Promise<{ ok: boolean; reason?: string }> {
  // authorization: VTR can only act on assigned tenants (VTAdmin unscoped).
  if (operator.assignedTenants !== null && !operator.assignedTenants.includes(tenantId)) {
    return { ok: false, reason: 'not assigned to this tenant' }
  }
  const newStatus = action === 'ack' ? 'ack' : 'resolved'
  const patch: Record<string, unknown> = { status: newStatus }
  if (newStatus === 'resolved') {
    patch.resolved_at = new Date().toISOString()
    patch.resolved_by = operator.operatorId
  }
  const { error } = await client.from('escalations').update(patch).eq('id', escalationId)
  if (error) return { ok: false, reason: String(error.message ?? error) }
  // append the ops-audit row (pure ops action — NOT privacy_audit_log).
  await client.from('ops_audit').insert({
    operator_id: operator.operatorId,
    tenant_id: tenantId,
    action,
    target_kind: 'escalation',
    target_id: escalationId,
    detail: note,
  })
  return { ok: true }
}
