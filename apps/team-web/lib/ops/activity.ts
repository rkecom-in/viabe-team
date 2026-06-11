/**
 * VT-293 — Activity / Pipelines: per-agent live activity, scoped to assigned tenants.
 *
 * Listing of recent/active runs (VT-290 scoping, fail-closed) + the per-run step stream
 * (timestamped step_kind / rationale / status / duration) shown in the overlay. fetchRunSteps
 * AUTHORIZES the run's tenant against the operator's assigned set before returning anything
 * (a VTR can't pull steps for an unassigned tenant by guessing a run_id). Deny-all-table
 * reads via serverSecretClient; scoping app-side.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'
import { forwardRunControl } from '@/lib/orchestrator-client'

type Client = { from: (t: string) => any }

interface OpsOperatorLike {
  role: OperatorRole
  assignedTenants: string[] | null
}

export interface ActivityRun {
  run_id: string
  tenant_id: string
  status: string
  started_at: string | null
}

export interface StepRow {
  step_index: number
  step_kind: string | null
  rationale: string | null
  status: string | null
  started_at: string | null
  duration_ms: number | null
}

function _since(hours: number): string {
  const d = new Date()
  d.setUTCHours(d.getUTCHours() - hours)
  return d.toISOString()
}

export async function fetchActiveRuns(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
  limit = 30,
): Promise<ActivityRun[]> {
  const { assignedTenants } = operator
  if (assignedTenants !== null && assignedTenants.length === 0) return [] // fail-closed
  let q = client
    .from('pipeline_runs')
    .select('id, tenant_id, status, started_at')
    .gte('started_at', _since(24))
    .order('started_at', { ascending: false })
    .limit(limit)
  if (assignedTenants !== null) q = q.in('tenant_id', assignedTenants)
  const { data } = await q
  return (data ?? []).map((r: any) => ({
    run_id: String(r.id),
    tenant_id: String(r.tenant_id),
    status: r.status,
    started_at: r.started_at,
  }))
}

/** The step stream for a run — AUTHORIZED: returns [] unless the run's tenant is in the
 *  operator's assigned set (VTAdmin unscoped). Fail-closed. */
export async function fetchRunSteps(
  operator: OpsOperatorLike,
  runId: string,
  client: Client = serverSecretClient(),
): Promise<StepRow[]> {
  const { assignedTenants } = operator
  // authorize: which tenant owns this run?
  const { data: runRows } = await client
    .from('pipeline_runs')
    .select('tenant_id')
    .eq('id', runId)
    .limit(1)
  const run = (runRows ?? [])[0] as { tenant_id: string } | undefined
  if (!run) return []
  if (assignedTenants !== null && !assignedTenants.includes(String(run.tenant_id))) {
    return [] // VTR not assigned to this run's tenant — fail-closed
  }
  const { data } = await client
    .from('pipeline_steps')
    .select('step_index, step_kind, rationale, status, started_at, duration_ms')
    .eq('run_id', runId)
    .order('step_index', { ascending: true })
  return (data ?? []).map((s: any) => ({
    step_index: s.step_index,
    step_kind: s.step_kind,
    rationale: s.rationale,
    status: s.status,
    started_at: s.started_at,
    duration_ms: s.duration_ms,
  }))
}

interface ActingOperator extends OpsOperatorLike {
  operatorId: string
}

function _authorized(op: ActingOperator, tenantId: string): boolean {
  return op.assignedTenants === null || op.assignedTenants.includes(tenantId)
}

/** Resolve a run's TRUE tenant from pipeline_runs. The run id is the only client input;
 *  the tenant is DERIVED — never trust a client-supplied tenant for authorization (IDOR). */
async function _resolveRunTenant(client: Client, runId: string): Promise<string | null> {
  const { data } = await client.from('pipeline_runs').select('tenant_id').eq('id', runId).limit(1)
  const run = (data ?? [])[0] as { tenant_id: string } | undefined
  return run ? String(run.tenant_id) : null
}

/** Escalate a run from the Activity view → writes an escalations row (idempotent on run)
 *  + an ops_audit entry. The run's tenant is RESOLVED server-side + authorized against it
 *  (no client-supplied tenant → no IDOR); the resolved tenant is what gets written. */
export async function escalateRun(
  op: ActingOperator, runId: string, client: Client = serverSecretClient(),
): Promise<{ ok: boolean; reason?: string }> {
  const tenantId = await _resolveRunTenant(client, runId)
  if (!tenantId) return { ok: false, reason: 'run not found' }
  if (!_authorized(op, tenantId)) return { ok: false, reason: 'not assigned to this tenant' }
  await client.from('escalations').upsert(
    { tenant_id: tenantId, run_id: runId, kind: 'agent_escalated', severity: 'medium' },
    { onConflict: 'run_id', ignoreDuplicates: true },
  )
  await client.from('ops_audit').insert({
    operator_id: op.operatorId, tenant_id: tenantId, action: 'escalate',
    target_kind: 'run', target_id: runId, detail: 'escalated from Activity',
  })
  return { ok: true }
}

/** VT-300 — issue a run-control on a live run. Fast team-web pre-check (resolve tenant
 *  server-side + assignment) for UX, then forward to the orchestrator's AUTHORITATIVE endpoint
 *  which re-derives the tenant + re-checks operator_assignments server-side (the enforcement
 *  leg — team-web auth alone is fail-open) + writes the hold + audits.
 *
 *  VT-374 (N1 retire): 'pause' now sets a tenant-wide campaign_send hold on the run-control
 *  substrate (released via the run-control API, not auto-expired). 'steer'/'override' were
 *  REMOVED from this legacy leg — the orchestrator returns 410; we surface that as a clear
 *  "moved to the Run-Control panel (Phase B)" reason rather than a generic failure (C6). */
export async function flagRunControl(
  op: ActingOperator, runId: string, control: string,
  client: Client = serverSecretClient(),
  forward: typeof forwardRunControl = forwardRunControl,
): Promise<{ ok: boolean; reason?: string }> {
  const tenantId = await _resolveRunTenant(client, runId)
  if (!tenantId) return { ok: false, reason: 'run not found' }
  if (!_authorized(op, tenantId)) return { ok: false, reason: 'not assigned to this tenant' }
  // Authoritative enforcement + audit happen in the orchestrator (re-derive + re-check).
  const res = await forward(op.operatorId, runId, control)
  if (res.ok) return { ok: true }
  // 410 = steer/override retired from this leg (VT-374). Give the operator the destination,
  // not a raw status code.
  if (res.reason === 'http_410') {
    return { ok: false, reason: 'Steer moved to the Run-Control panel (Phase B)' }
  }
  return { ok: false, reason: res.reason }
}
