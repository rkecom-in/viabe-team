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
import type { MaskedOpsRow } from '@/lib/ops/de-identify'
import { fetchVtrEscalations } from '@/lib/orchestrator-client'

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
  const { assignedTenants, role } = operator
  if (assignedTenants !== null && assignedTenants.length === 0) return [] // fail-closed
  // VT-360: the VTR surface is DB-enforced — read the de-identified vtr_escalations view through
  // the orchestrator (app_vtr_role; route='vtr' + unresolved filtered server-side). NO raw table,
  // NO app-side masking. VTAdmin (operator full-access) keeps the service-role read.
  if (role !== OperatorRole.VTADMIN) {
    const rows = await fetchVtrEscalations(operator.operatorId)
    return rows.slice(0, limit).map((r) => ({
      id: String(r.escalation_id),
      tenant_id: String(r.tenant_id),
      tenant_name: (r.tenant_name as string | null) ?? null,
      reference: String(r.escalation_id), // operational row handle (early-review F2 — raw UUID OK)
      severity: (r.severity as string | null) ?? null,
      kind: (r.kind as string | null) ?? null,
      time: (r.opened_at as string | null) ?? null,
      status: (r.status as string | null) ?? null,
    }))
  }
  let q = client
    .from('escalations')
    .select('id, tenant_id, kind, severity, status, opened_at')
    .neq('status', 'resolved')
    .order('opened_at', { ascending: false })
    .limit(limit)
  if (assignedTenants !== null) q = q.in('tenant_id', assignedTenants)
  const { data } = await q
  return ((data ?? []) as any[]).map((r) => ({
    id: String(r.id),
    tenant_id: String(r.tenant_id),
    tenant_name: null,
    reference: String(r.id), // operational row handle (referenceFor deleted — escalations carry no PII)
    severity: r.severity ?? null,
    kind: r.kind ?? null,
    time: r.opened_at ?? null,
    status: r.status ?? null,
  }))
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
