/**
 * VT-704 — the per-tenant ACTIVITY FLOW (Fazal 2026-07-24: "a complete flow for past 30 days
 * showing conversations with tenant, decision, action, triggers, events, invoking sub-agents,
 * executions by sub-agents, inputs, outputs, errors, concerns … as a time based flow").
 *
 * Unions eight orchestrator streams into ONE time-ordered event list per tenant:
 * conversation_log (owner ↔ Manager turns), tm_audit_log (the Manager's decide/act spine),
 * manager_tasks (sub-agent dispatches + outcomes), pipeline_steps (execution ERRORS only —
 * healthy steps are volume, not signal), pending_approvals (asks + resolutions),
 * owner_comms_queue (delivered/dropped comms), incidents + tenant_alerts (concerns).
 *
 * Access: serverSecretClient reads with APP-SIDE scoping (VT-290 pattern) — fail-closed on an
 * empty assigned set; a VTR only ever reads assigned tenants (canAccessTenant at the callsite
 * AND here). Per-source caps are surfaced in `counts` — truncation is never silent.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'
import { canAccessTenant } from '@/lib/ops/assignments'

type Client = { from: (t: string) => any }

interface OpsOperatorLike {
  role: OperatorRole
  assignedTenants: string[] | null
}

export type FlowLane = 'owner' | 'assistant' | 'system'
export type FlowKind =
  | 'message'
  | 'decision'
  | 'task'
  | 'step_error'
  | 'approval'
  | 'comms'
  | 'incident'
  | 'alert'
export type FlowSeverity = 'info' | 'warn' | 'error'

export interface FlowEvent {
  ts: string
  lane: FlowLane
  kind: FlowKind
  title: string
  body: string
  severity: FlowSeverity
  meta: Record<string, string>
}

export interface FlowResult {
  events: FlowEvent[]
  /** rows fetched per source (each capped) — render the caps, never hide them */
  counts: Record<string, { fetched: number; cap: number }>
}

const CAPS = {
  conversation_log: 600,
  tm_audit_log: 500,
  manager_tasks: 200,
  pipeline_steps: 200,
  pending_approvals: 100,
  owner_comms_queue: 150,
  incidents: 100,
  tenant_alerts: 100,
} as const

function sinceIso(days: number): string {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - days)
  return d.toISOString()
}

function s(v: unknown, max = 400): string {
  if (v == null) return ''
  const str = typeof v === 'string' ? v : JSON.stringify(v)
  return str.length > max ? `${str.slice(0, max)}…` : str
}

function sevFrom(raw: unknown, fallback: FlowSeverity = 'info'): FlowSeverity {
  const v = String(raw ?? '').toLowerCase()
  if (['error', 'critical', 'high', 'sev1'].includes(v)) return 'error'
  if (['warn', 'warning', 'medium', 'sev2'].includes(v)) return 'warn'
  return fallback
}

async function grab(
  client: Client,
  table: keyof typeof CAPS,
  select: string,
  tsCol: string,
  tenantId: string,
  since: string,
): Promise<any[]> {
  const { data } = await client
    .from(table)
    .select(select)
    .eq('tenant_id', tenantId)
    .gte(tsCol, since)
    .order(tsCol, { ascending: false })
    .limit(CAPS[table])
  return data ?? []
}

export async function fetchTenantFlow(
  operator: OpsOperatorLike,
  tenantId: string,
  opts: { days?: number; client?: Client } = {},
): Promise<FlowResult> {
  const { assignedTenants } = operator
  if (assignedTenants !== null && assignedTenants.length === 0)
    return { events: [], counts: {} } // fail-closed
  if (!canAccessTenant(assignedTenants, tenantId)) return { events: [], counts: {} }
  const client = opts.client ?? serverSecretClient()
  const since = sinceIso(opts.days ?? 30)

  const [convo, audit, tasks, stepErrs, approvals, comms, incidents, alerts] = await Promise.all([
    grab(client, 'conversation_log', 'role, text, surface, created_at', 'created_at', tenantId, since),
    grab(
      client,
      'tm_audit_log',
      'event_layer, event_kind, actor, summary, decision, action, result, severity, status, run_id, created_at',
      'created_at',
      tenantId,
      since,
    ),
    grab(
      client,
      'manager_tasks',
      'objective, assigned_function, status, terminal_outcome, created_at, completed_at',
      'created_at',
      tenantId,
      since,
    ),
    (async () => {
      const { data } = await client
        .from('pipeline_steps')
        .select('step_name, step_kind, error, status, started_at, run_id')
        .eq('tenant_id', tenantId)
        .gte('started_at', since)
        .not('error', 'is', null)
        .order('started_at', { ascending: false })
        .limit(CAPS.pipeline_steps)
      return data ?? []
    })(),
    grab(
      client,
      'pending_approvals',
      'approval_type, summary, status, decision, requested_at, resolved_at',
      'requested_at',
      tenantId,
      since,
    ),
    grab(
      client,
      'owner_comms_queue',
      'kind, status, dropped_reason, queued_at, delivered_at, payload',
      'queued_at',
      tenantId,
      since,
    ),
    grab(client, 'incidents', 'incident_kind, severity, status, detail, created_at', 'created_at', tenantId, since),
    grab(client, 'tenant_alerts', 'trigger_kind, severity, message_text, fired_at', 'fired_at', tenantId, since),
  ])

  const events: FlowEvent[] = []

  for (const r of convo) {
    events.push({
      ts: r.created_at,
      lane: r.role === 'owner' ? 'owner' : 'assistant',
      kind: 'message',
      title: r.role === 'owner' ? 'Owner' : 'Manager',
      body: s(r.text, 1200),
      severity: 'info',
      meta: { surface: s(r.surface, 40) },
    })
  }
  for (const r of audit) {
    const parts = [
      r.decision ? `decided: ${s(r.decision, 240)}` : '',
      r.action ? `did: ${s(r.action, 240)}` : '',
      r.result ? `result: ${s(r.result, 240)}` : '',
    ].filter(Boolean)
    events.push({
      ts: r.created_at,
      lane: 'system',
      kind: 'decision',
      title: `${s(r.actor, 40) || 'manager'} · ${s(r.event_kind, 60)}`,
      body: [s(r.summary, 300), ...parts].filter(Boolean).join('\n'),
      severity: sevFrom(r.severity),
      meta: { layer: s(r.event_layer, 30), run: s(r.run_id, 40), status: s(r.status, 30) },
    })
  }
  for (const r of tasks) {
    events.push({
      ts: r.created_at,
      lane: 'system',
      kind: 'task',
      title: `sub-agent: ${s(r.assigned_function, 60) || 'manager task'}`,
      body: s(r.objective, 400),
      severity: r.terminal_outcome === 'failed' ? 'error' : 'info',
      meta: { status: s(r.status, 30), outcome: s(r.terminal_outcome, 40), done: s(r.completed_at, 30) },
    })
  }
  for (const r of stepErrs) {
    events.push({
      ts: r.started_at,
      lane: 'system',
      kind: 'step_error',
      title: `step failed: ${s(r.step_name || r.step_kind, 60)}`,
      body: s(r.error, 500),
      severity: 'error',
      meta: { run: s(r.run_id, 40), status: s(r.status, 30) },
    })
  }
  for (const r of approvals) {
    events.push({
      ts: r.requested_at,
      lane: 'system',
      kind: 'approval',
      title: `approval asked: ${s(r.approval_type, 50)}`,
      body: s(r.summary, 400),
      severity: r.status === 'expired' ? 'warn' : 'info',
      meta: { status: s(r.status, 30), decision: s(r.decision, 30), resolved: s(r.resolved_at, 30) },
    })
    if (r.resolved_at) {
      events.push({
        ts: r.resolved_at,
        lane: 'system',
        kind: 'approval',
        title: `approval ${s(r.decision, 30) || r.status}`,
        body: s(r.summary, 200),
        severity: 'info',
        meta: { status: s(r.status, 30) },
      })
    }
  }
  for (const r of comms) {
    const delivered = Boolean(r.delivered_at)
    const dropped = r.status === 'dropped'
    events.push({
      ts: r.delivered_at ?? r.queued_at,
      lane: 'system',
      kind: 'comms',
      title: dropped
        ? `comms dropped (${s(r.dropped_reason, 40) || 'no reason'})`
        : delivered
          ? `comms delivered: ${s(r.kind, 30)}`
          : `comms queued: ${s(r.kind, 30)}`,
      body: s(r.payload?.text_en ?? r.payload, 300),
      severity: dropped ? 'warn' : 'info',
      meta: { status: s(r.status, 20) },
    })
  }
  for (const r of incidents) {
    events.push({
      ts: r.created_at,
      lane: 'system',
      kind: 'incident',
      title: `incident: ${s(r.incident_kind, 60)}`,
      body: s(r.detail, 400),
      severity: sevFrom(r.severity, 'warn'),
      meta: { status: s(r.status, 30) },
    })
  }
  for (const r of alerts) {
    events.push({
      ts: r.fired_at,
      lane: 'system',
      kind: 'alert',
      title: `alert: ${s(r.trigger_kind, 60)}`,
      body: s(r.message_text, 300),
      severity: sevFrom(r.severity, 'warn'),
      meta: {},
    })
  }

  events.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0))
  return {
    events,
    counts: {
      conversation_log: { fetched: convo.length, cap: CAPS.conversation_log },
      tm_audit_log: { fetched: audit.length, cap: CAPS.tm_audit_log },
      manager_tasks: { fetched: tasks.length, cap: CAPS.manager_tasks },
      pipeline_steps: { fetched: stepErrs.length, cap: CAPS.pipeline_steps },
      pending_approvals: { fetched: approvals.length, cap: CAPS.pending_approvals },
      owner_comms_queue: { fetched: comms.length, cap: CAPS.owner_comms_queue },
      incidents: { fetched: incidents.length, cap: CAPS.incidents },
      tenant_alerts: { fetched: alerts.length, cap: CAPS.tenant_alerts },
    },
  }
}

/** Group a sorted event list by UTC day for the day-header render. */
export function groupByDay(events: FlowEvent[]): Array<{ day: string; events: FlowEvent[] }> {
  const out: Array<{ day: string; events: FlowEvent[] }> = []
  for (const e of events) {
    const day = String(e.ts).slice(0, 10)
    const last = out[out.length - 1]
    if (last && last.day === day) last.events.push(e)
    else out.push({ day, events: [e] })
  }
  return out
}
