/**
 * VT-291 — Fleet: per-business agent health, scoped to the operator's assigned tenants.
 *
 * Reuses the VT-290 contract: assignment scoping (VTR → assigned only, fail-closed;
 * VTAdmin → all) + the role model. Derived from pipeline_runs status markers (last 24h),
 * aggregated per tenant. No new migration. Health is a pure derivation (testable).
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'

export type FleetHealth = 'green' | 'yellow' | 'red'

export interface FleetRow {
  tenant_id: string
  tenant_name: string | null
  running: number
  escalated: number
  hard_limits: number
  health: FleetHealth
}

interface OpsOperatorLike {
  role: OperatorRole
  assignedTenants: string[] | null
}

type Client = { from: (t: string) => any }

/** Pure health rule: any hard-limit/escalation → red; in-flight only → yellow; else green. */
export function deriveHealth(counts: { escalated: number; hard_limits: number; running: number }): FleetHealth {
  if (counts.hard_limits > 0 || counts.escalated > 0) return 'red'
  if (counts.running > 0) return 'yellow'
  return 'green'
}

function _since24h(): string {
  const d = new Date()
  d.setUTCHours(d.getUTCHours() - 24)
  return d.toISOString()
}

export async function fetchFleet(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
): Promise<FleetRow[]> {
  const { assignedTenants } = operator
  // fail-CLOSED: a VTR with no assignments sees nothing.
  if (assignedTenants !== null && assignedTenants.length === 0) return []

  const since = _since24h()
  let q = client
    .from('pipeline_runs')
    .select('tenant_id, status')
    .gte('started_at', since)
  if (assignedTenants !== null) q = q.in('tenant_id', assignedTenants)
  const { data: runs } = await q

  // aggregate per tenant in JS (avoids an RPC; the scoped row set is small per operator).
  const byTenant = new Map<string, { escalated: number; hard_limits: number; running: number }>()
  for (const r of (runs ?? []) as { tenant_id: string; status: string }[]) {
    const agg = byTenant.get(r.tenant_id) ?? { escalated: 0, hard_limits: 0, running: 0 }
    if (r.status === 'escalated') agg.escalated += 1
    else if (r.status === 'aborted_hard_limit') agg.hard_limits += 1
    else if (r.status === 'running') agg.running += 1
    byTenant.set(r.tenant_id, agg)
  }

  // tenant display names (scoped to the same set).
  const ids = [...byTenant.keys()]
  const names = new Map<string, string | null>()
  if (ids.length > 0) {
    const { data: tenants } = await client.from('tenants').select('id, business_name').in('id', ids)
    for (const t of (tenants ?? []) as { id: string; business_name: string | null }[]) {
      names.set(t.id, t.business_name)
    }
  }

  return ids.map((tid) => {
    const c = byTenant.get(tid)!
    return {
      tenant_id: tid,
      tenant_name: names.get(tid) ?? null,
      running: c.running,
      escalated: c.escalated,
      hard_limits: c.hard_limits,
      health: deriveHealth(c),
    }
  })
}
