/**
 * VT-290 — Home/Triage data (urgency-first), scoped + de-identified.
 *
 * Derives the queue from pipeline_runs status markers (escalated / aborted_hard_limit)
 * for v1 — a SEAM: when VT-292 builds the real `escalations` table, repoint here. Scoped
 * to the operator's assigned tenant set (VTR) or unscoped (VTAdmin), fail-CLOSED for a
 * VTR with no assignments. Rows are de-identified for VTR (CL-426).
 *
 * Every KPI tile carries a `href` → a real listing+filter (nothing dead-ended).
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'
import type { MaskedOpsRow } from '@/lib/ops/de-identify'
import { fetchEscalations } from '@/lib/ops/escalations'

export interface KpiTile {
  key: string
  label: string
  count: number
  href: string
}

export interface HomeTriageData {
  role: OperatorRole
  kpis: KpiTile[]
  escalations: MaskedOpsRow[]
  scoped: boolean // true = VTR (filtered); false = VTAdmin (all)
}

type Client = { from: (t: string) => any }

function _since24h(): string {
  const d = new Date()
  d.setUTCHours(d.getUTCHours() - 24)
  return d.toISOString()
}

/** Apply VTR tenant-scoping to a query builder. assignedTenants===null → unscoped. */
function _scope(q: any, assignedTenants: string[] | null) {
  return assignedTenants === null ? q : q.in('tenant_id', assignedTenants)
}

async function _count(client: Client, status: string, assignedTenants: string[] | null, since: string): Promise<number> {
  // VTR with zero assignments: fail-closed (no query, count 0).
  if (assignedTenants !== null && assignedTenants.length === 0) return 0
  const base = client.from('pipeline_runs').select('id', { count: 'exact', head: true }).eq('status', status).gte('started_at', since)
  const { count } = await _scope(base, assignedTenants)
  return count ?? 0
}

export async function fetchHomeTriage(
  operator: { role: OperatorRole; assignedTenants: string[] | null },
  client: Client = serverSecretClient(),
): Promise<HomeTriageData> {
  const { role, assignedTenants } = operator
  const since = _since24h()

  const [escalated, hardLimits, running] = await Promise.all([
    _count(client, 'escalated', assignedTenants, since),
    _count(client, 'aborted_hard_limit', assignedTenants, since),
    _count(client, 'running', assignedTenants, since),
  ])

  const kpis: KpiTile[] = [
    { key: 'escalated', label: 'Escalations (24h)', count: escalated, href: '/team/ops/escalations?status=escalated' },
    { key: 'hard_limits', label: 'Hard limits (24h)', count: hardLimits, href: '/team/ops/escalations?status=aborted_hard_limit' },
    { key: 'in_flight', label: 'In-flight agents', count: running, href: '/team/ops/activity?status=running' },
  ]

  // Escalation snippet — VT-292 repoint: now reads the canonical `escalations` table (was
  // the pipeline_runs seam). fetchEscalations does the scoping + de-identification.
  let escalations: MaskedOpsRow[] = []
  const noAccess = assignedTenants !== null && assignedTenants.length === 0
  if (!noAccess) {
    escalations = await fetchEscalations({ operatorId: '', role, assignedTenants }, client, 5)
  }

  return { role, kpis, escalations, scoped: assignedTenants !== null }
}
