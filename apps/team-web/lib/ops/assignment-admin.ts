/**
 * VT-295 — Ops Console V2 Assignment management (VTAdmin only).
 *
 * VTAdmin CRUD over operator_assignments (migration 072): list all businesses + their
 * active VTRs, assign / reassign / unassign. The assignment is the source of truth every
 * other page's VTR→tenant scoping reads from (lib/ops/assignments.ts resolveAssignedTenants);
 * a change here takes effect on the VTR's NEXT request (no client cache).
 *
 * Authorization model (binding, mirrors the VT-293/294 IDOR fixes):
 *   - Every read + mutation is VTAdmin-only, fail-CLOSED. A non-VTAdmin caller gets an
 *     empty set / {ok:false} and writes NOTHING. The role is resolved server-side by
 *     requireOpsOperator (never a client field).
 *   - unassign() derives the target's operator_id + tenant_id from the assignment ROW
 *     (by id) server-side — never from a client-supplied scoping field. assign() validates
 *     the tenant + operator exist (active) before writing.
 *   - operator_assignments is deny-all FORCE RLS → serverSecretClient (service-role,
 *     RLS-bypassing); the VTAdmin gate is the access control.
 *
 * Every mutation appends an ops_audit row (migration 074, Pillar 7). CL-390: operator/tenant
 * are bare UUIDs (no PII); detail carries no PII.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole, isVtAdmin } from '@/lib/auth/roles'

type Client = { from: (t: string) => any }

interface OpsOperatorLike {
  operatorId: string
  role: OperatorRole
  assignedTenants: string[] | null
}

export interface ActiveAssignment {
  /** operator_assignments row id — the handle unassign() resolves its target from. */
  assignment_id: string
  /** the assigned VTR (bare UUID — no PII). */
  operator_id: string
}

export interface BusinessAssignment {
  tenant_id: string
  business_name: string | null
  /** active VTR assignments for this business. */
  assignments: ActiveAssignment[]
}

export interface AssignableOperator {
  operator_id: string
}

export type AssignmentResult = { ok: boolean; reason?: string }

/** Fail-closed VTAdmin gate. Returns null when authorized; a refusal otherwise. */
function denyIfNotAdmin(operator: OpsOperatorLike): AssignmentResult | null {
  return isVtAdmin(operator.role) ? null : { ok: false, reason: 'VTAdmin only' }
}

/** All businesses + their active VTR assignments (VTAdmin only; [] for anyone else). */
export async function fetchAllBusinesses(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
): Promise<BusinessAssignment[]> {
  if (!isVtAdmin(operator.role)) return [] // fail-closed: non-admin sees nothing here
  const { data: tenants, error: tErr } = await client
    .from('tenants')
    .select('id, business_name')
    .order('business_name', { ascending: true })
  if (tErr) {
    console.error('fetchAllBusinesses: tenants query failed', tErr)
    return []
  }
  const { data: rows, error: aErr } = await client
    .from('operator_assignments')
    .select('id, tenant_id, operator_id')
    .is('unassigned_at', null)
  if (aErr) {
    console.error('fetchAllBusinesses: assignments query failed', aErr)
    return []
  }
  const byTenant = new Map<string, ActiveAssignment[]>()
  for (const r of (rows ?? []) as { id: string; tenant_id: string; operator_id: string }[]) {
    const list = byTenant.get(String(r.tenant_id)) ?? []
    list.push({ assignment_id: String(r.id), operator_id: String(r.operator_id) })
    byTenant.set(String(r.tenant_id), list)
  }
  return ((tenants ?? []) as { id: string; business_name: string | null }[]).map((t) => ({
    tenant_id: String(t.id),
    business_name: t.business_name ?? null,
    assignments: byTenant.get(String(t.id)) ?? [],
  }))
}

/** Active operators eligible to be assigned (VTAdmin only). Bare UUIDs (CL-390). */
export async function fetchAssignableOperators(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
): Promise<AssignableOperator[]> {
  if (!isVtAdmin(operator.role)) return []
  const { data, error } = await client
    .from('operator_allowlist')
    .select('user_id')
    .is('revoked_at', null)
  if (error) {
    console.error('fetchAssignableOperators: query failed', error)
    return []
  }
  return ((data ?? []) as { user_id: string }[]).map((r) => ({ operator_id: String(r.user_id) }))
}

/** Assign a business to a VTR (VTAdmin only). Validates the tenant + operator exist
 *  (active) before writing; idempotent on an existing active assignment. Audits 'assign'. */
export async function assignBusiness(
  operator: OpsOperatorLike,
  tenantId: string,
  operatorId: string,
  note: string | null = null,
  client: Client = serverSecretClient(),
): Promise<AssignmentResult> {
  const denied = denyIfNotAdmin(operator)
  if (denied) return denied
  if (!tenantId || !operatorId) return { ok: false, reason: 'tenantId and operatorId required' }

  // Validate both targets server-side (never trust the client beyond the ids themselves).
  const { data: tenant } = await client.from('tenants').select('id').eq('id', tenantId).maybeSingle()
  if (!tenant) return { ok: false, reason: 'unknown tenant' }
  const { data: op } = await client
    .from('operator_allowlist')
    .select('user_id')
    .eq('user_id', operatorId)
    .is('revoked_at', null)
    .maybeSingle()
  if (!op) return { ok: false, reason: 'unknown or revoked operator' }

  // Idempotent: skip if an active assignment already exists (partial-unique would reject it).
  const { data: existing } = await client
    .from('operator_assignments')
    .select('id')
    .eq('tenant_id', tenantId)
    .eq('operator_id', operatorId)
    .is('unassigned_at', null)
    .maybeSingle()
  if (existing) return { ok: true, reason: 'already assigned' }

  const { error } = await client.from('operator_assignments').insert({
    tenant_id: tenantId,
    operator_id: operatorId,
    assigned_by: operator.operatorId,
    notes: note,
  })
  if (error) return { ok: false, reason: String(error.message ?? error) }

  await client.from('ops_audit').insert({
    operator_id: operator.operatorId,
    tenant_id: tenantId,
    action: 'assign',
    target_kind: 'assignment',
    target_id: operatorId, // the VTR receiving the business
    detail: note,
  })
  return { ok: true }
}

/** Revoke an assignment by its ROW id (VTAdmin only). The target operator_id + tenant_id
 *  are resolved FROM the row server-side (IDOR rule: never from a client scoping field).
 *  Sets unassigned_at; audits 'unassign' against the resolved tenant/operator. */
export async function unassignBusiness(
  operator: OpsOperatorLike,
  assignmentId: string,
  note: string | null = null,
  client: Client = serverSecretClient(),
): Promise<AssignmentResult> {
  const denied = denyIfNotAdmin(operator)
  if (denied) return denied
  if (!assignmentId) return { ok: false, reason: 'assignmentId required' }

  // Resolve the target from the ROW — the assignmentId is the only client input.
  const { data: row } = await client
    .from('operator_assignments')
    .select('id, operator_id, tenant_id, unassigned_at')
    .eq('id', assignmentId)
    .maybeSingle()
  if (!row) return { ok: false, reason: 'assignment not found' }
  if ((row as { unassigned_at: string | null }).unassigned_at) {
    return { ok: false, reason: 'already unassigned' }
  }
  const resolved = row as { operator_id: string; tenant_id: string }

  const { error } = await client
    .from('operator_assignments')
    .update({ unassigned_at: new Date().toISOString() })
    .eq('id', assignmentId)
  if (error) return { ok: false, reason: String(error.message ?? error) }

  await client.from('ops_audit').insert({
    operator_id: operator.operatorId,
    tenant_id: resolved.tenant_id, // server-resolved, not client-supplied
    action: 'unassign',
    target_kind: 'assignment',
    target_id: resolved.operator_id,
    detail: note,
  })
  return { ok: true }
}
