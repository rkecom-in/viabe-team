/**
 * Server-side data-access helpers for the Ops Console (VT-123).
 *
 * All queries run via `serverServiceRoleClient()` (CL-52 cold-read
 * pattern); RLS is bypassed by service-role. Callers MUST have already
 * passed `requireFazal()` before invoking these.
 *
 * Per CL-417: every projection over `pipeline_steps` returns canonical
 * per-field columns (NOT JSONB extraction) so the run-replay view
 * renders native columns directly.
 * Per CL-416: lifetime retention — no `WHERE deleted_at IS NULL`
 * predicates; rollups are a separate VT-N.
 */

import { serverServiceRoleClient } from '@/lib/supabase-client'

export interface WorkspaceCounters {
  in_flight_runs: number
  total_runs_today: number
  escalations_today: number
  cost_burn_today_paise: number
}

export interface TopTenantRow {
  tenant_id: string
  business_name: string | null
  runs_count: number
}

export interface InFlightRun {
  run_id: string
  tenant_id: string
  status: string
  started_at: string
}

export interface TenantProfile {
  tenant_id: string
  business_name: string | null
  plan_tier: string
  phase: string
}

export interface TenantTimelineEntry {
  run_id: string
  status: string
  started_at: string
  ended_at: string | null
  trigger_kind: string | null
  total_cost_paise: number | null
  step_count: number | null
}

export interface RecentCampaign {
  campaign_id: string
  status: string
  generated_at: string
}

export interface PrivacyAuditEntry {
  id: string
  event_type: string
  actor: string | null
  created_at: string
  payload: Record<string, unknown>
}

export interface PipelineStepRow {
  id: string
  run_id: string
  step_seq: number
  step_kind: string
  step_name: string | null
  parent_step_id: string | null
  status: string
  decision_rationale: string | null
  model_used: string | null
  tokens_input: number | null
  tokens_output: number | null
  cost_paise: number | null
  duration_ms: number | null
  tool_calls: unknown
  input_envelope: unknown
  output_envelope: unknown
  error: unknown
  started_at: string
  ended_at: string | null
}


export async function fetchWorkspaceCounters(): Promise<WorkspaceCounters> {
  const client = serverServiceRoleClient()
  const todayStart = new Date()
  todayStart.setUTCHours(0, 0, 0, 0)
  const todayIso = todayStart.toISOString()

  const [inFlight, totalToday, escalations, costBurn] = await Promise.all([
    client
      .from('pipeline_runs')
      .select('id', { count: 'exact', head: true })
      .eq('status', 'running'),
    client
      .from('pipeline_runs')
      .select('id', { count: 'exact', head: true })
      .gte('started_at', todayIso),
    client
      .from('pipeline_steps')
      .select('id', { count: 'exact', head: true })
      .eq('step_kind', 'agent_reasoning_step')
      .like('step_name', '%escalate_to_fazal%')
      .gte('started_at', todayIso),
    client
      .from('pipeline_runs')
      .select('total_cost_paise')
      .gte('started_at', todayIso),
  ])

  const costSum = (costBurn.data ?? []).reduce(
    (acc, row) => acc + Number(row.total_cost_paise ?? 0),
    0,
  )

  return {
    in_flight_runs: inFlight.count ?? 0,
    total_runs_today: totalToday.count ?? 0,
    escalations_today: escalations.count ?? 0,
    cost_burn_today_paise: costSum,
  }
}


export async function fetchTopTenants(limit = 10): Promise<TopTenantRow[]> {
  const client = serverServiceRoleClient()
  const todayStart = new Date()
  todayStart.setUTCHours(0, 0, 0, 0)
  const { data } = await client.rpc('ops_top_tenants_today', {
    p_limit: limit,
    p_since: todayStart.toISOString(),
  })
  return ((data as TopTenantRow[]) ?? [])
}


export async function fetchInFlightRuns(limit = 20): Promise<InFlightRun[]> {
  const client = serverServiceRoleClient()
  const { data } = await client
    .from('pipeline_runs')
    .select('id, tenant_id, status, started_at')
    .eq('status', 'running')
    .order('started_at', { ascending: false })
    .limit(limit)
  return ((data ?? []) as Array<{
    id: string
    tenant_id: string
    status: string
    started_at: string
  }>).map((r) => ({
    run_id: r.id,
    tenant_id: r.tenant_id,
    status: r.status,
    started_at: r.started_at,
  }))
}


export async function fetchTenantProfile(
  tenantId: string,
): Promise<TenantProfile | null> {
  const client = serverServiceRoleClient()
  const { data } = await client
    .from('tenants')
    .select('id, business_name, plan_tier, phase')
    .eq('id', tenantId)
    .maybeSingle()
  if (!data) return null
  return {
    tenant_id: data.id,
    business_name: data.business_name,
    plan_tier: data.plan_tier,
    phase: data.phase,
  }
}


export async function fetchTenantTimeline(
  tenantId: string,
  days = 30,
): Promise<TenantTimelineEntry[]> {
  const client = serverServiceRoleClient()
  const since = new Date()
  since.setUTCDate(since.getUTCDate() - days)
  const { data } = await client
    .from('pipeline_runs')
    .select(
      'id, status, started_at, ended_at, trigger_kind, total_cost_paise, step_count',
    )
    .eq('tenant_id', tenantId)
    .gte('started_at', since.toISOString())
    .order('started_at', { ascending: false })
    .limit(500)
  return ((data ?? []) as Array<{
    id: string
    status: string
    started_at: string
    ended_at: string | null
    trigger_kind: string | null
    total_cost_paise: number | null
    step_count: number | null
  }>).map((r) => ({
    run_id: r.id,
    status: r.status,
    started_at: r.started_at,
    ended_at: r.ended_at,
    trigger_kind: r.trigger_kind,
    total_cost_paise: r.total_cost_paise,
    step_count: r.step_count,
  }))
}


export async function fetchRecentCampaigns(
  tenantId: string,
  limit = 5,
): Promise<RecentCampaign[]> {
  const client = serverServiceRoleClient()
  const { data } = await client
    .from('campaigns')
    .select('id, status, generated_at')
    .eq('tenant_id', tenantId)
    .order('generated_at', { ascending: false })
    .limit(limit)
  return ((data ?? []) as Array<{
    id: string
    status: string
    generated_at: string
  }>).map((r) => ({
    campaign_id: r.id,
    status: r.status,
    generated_at: r.generated_at,
  }))
}


export async function fetchPrivacyAudit(
  tenantId: string,
  limit = 20,
): Promise<PrivacyAuditEntry[]> {
  const client = serverServiceRoleClient()
  const { data } = await client
    .from('privacy_audit_log')
    .select('id, event_type, actor, created_at, payload')
    .eq('tenant_id', tenantId)
    .order('created_at', { ascending: false })
    .limit(limit)
  return ((data ?? []) as Array<{
    id: string
    event_type: string
    actor: string | null
    created_at: string
    payload: Record<string, unknown>
  }>)
}


export async function fetchRunReplay(
  runId: string,
): Promise<PipelineStepRow[]> {
  const client = serverServiceRoleClient()
  const { data } = await client
    .from('pipeline_steps')
    .select(
      'id, run_id, step_seq, step_kind, step_name, parent_step_id, status, ' +
        'decision_rationale, model_used, tokens_input, tokens_output, ' +
        'cost_paise, duration_ms, tool_calls, input_envelope, output_envelope, ' +
        'error, started_at, ended_at',
    )
    .eq('run_id', runId)
    .order('step_seq', { ascending: true })
  return ((data ?? []) as unknown as PipelineStepRow[])
}
