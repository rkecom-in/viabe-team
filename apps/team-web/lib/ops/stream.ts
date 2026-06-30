/**
 * VT-201 Ops live-stream subscription helper.
 *
 * Browser-side subscription to Supabase Realtime channel that broadcasts
 * pipeline_steps INSERTs. Filtered + auth-gated via operator-claim JWT
 * (VT-188 substrate; Phase-1 single operator = Fazal).
 *
 * Per Q2 Option B locked at Cowork plan-review 2026-05-27 (Phase-1
 * only): direct browser subscription. Phase 2 with multiple operators
 * migrates to server-side SSE proxy (Q2 Option A). When that migrates,
 * this module's public surface stays the same — internal
 * implementation switches from Supabase Realtime to SSE consumer.
 *
 * Per CL-88 client-direct JWT pattern.
 * Per CL-417 canonical per-field columns: stream event shape mirrors
 * pipeline_steps columns 1:1.
 */

import {
  createClient,
  type RealtimePostgresInsertPayload,
  type SupabaseClient,
} from '@supabase/supabase-js'

export interface PipelineStepEvent {
  id: string
  run_id: string
  tenant_id: string
  step_seq: number
  step_kind: string
  step_name: string | null
  status: string
  decision_rationale: string | null
  model_used: string | null
  tokens_input: number | null
  tokens_output: number | null
  cost_paise: number | null
  duration_ms: number | null
  input_envelope: unknown
  output_envelope: unknown
  started_at: string
}

export interface StreamFilters {
  tenantIds?: string[]
  stepKinds?: string[]
  statuses?: string[]
}

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? ''
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? ''


export function browserStreamClient(operatorJwt: string): SupabaseClient {
  if (!SUPABASE_URL || !PUBLISHABLE_KEY) {
    throw new Error(
      'browserStreamClient: NEXT_PUBLIC_SUPABASE_URL + ' +
        'NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY must be set',
    )
  }
  const client = createClient(SUPABASE_URL, PUBLISHABLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: {
      headers: { Authorization: `Bearer ${operatorJwt}` },
    },
    realtime: {
      params: { eventsPerSecond: 50 },
    },
  })
  // Wire operator JWT into Realtime auth for RLS evaluation in the
  // pipeline_steps_operator_select policy from migration 030.
  client.realtime.setAuth(operatorJwt)
  return client
}


export function subscribePipelineSteps(
  client: SupabaseClient,
  filters: StreamFilters,
  onEvent: (step: PipelineStepEvent) => void,
): () => void {
  const channel = client
    .channel('ops:pipeline_steps')
    .on(
      'postgres_changes' as never,
      { event: 'INSERT', schema: 'public', table: 'pipeline_steps' },
      (payload: RealtimePostgresInsertPayload<PipelineStepEvent>) => {
        const row = payload.new
        if (!_matchesFilters(row, filters)) return
        onEvent(row)
      },
    )
    .subscribe()
  return () => {
    void client.removeChannel(channel)
  }
}


function _matchesFilters(
  step: PipelineStepEvent,
  filters: StreamFilters,
): boolean {
  if (filters.tenantIds?.length && !filters.tenantIds.includes(step.tenant_id)) {
    return false
  }
  if (filters.stepKinds?.length && !filters.stepKinds.includes(step.step_kind)) {
    return false
  }
  if (filters.statuses?.length && !filters.statuses.includes(step.status)) {
    return false
  }
  return true
}


// ─── Debug Events ──────────────────────────────────────────────────────────────
// VT-515: Supabase Realtime subscription for debug_events INSERTs.
// Mirrors the subscribePipelineSteps pattern above.

export interface DebugEvent {
  id: string
  created_at: string
  tenant_id: string | null
  trace_id: string | null
  failure_type: 'exception' | 'timeout' | 'vendor_error' | 'network' | 'validation' | 'crash' | 'silent_degrade'
  component: string
  operation: string | null
  error_message: string | null
  error_stack: string | null
  context: unknown
  severity: 'warning' | 'error' | 'critical'
  impact: string | null
  vendor: string | null
  vendor_status: string | null
  latency_ms: number | null
}

export interface DebugEventFilters {
  /** Exact tenant_id match; null events (pre-tenant) still show when omitted. */
  tenant_id?: string
  /** Exact component match (e.g. 'discovery', 'create'). */
  component?: string
  /** Exact severity match: 'warning' | 'error' | 'critical'. */
  severity?: string
}

export function subscribeDebugEvents(
  client: SupabaseClient,
  filters: DebugEventFilters,
  onEvent: (event: DebugEvent) => void,
): () => void {
  const channel = client
    .channel('ops:debug_events')
    .on(
      'postgres_changes' as never,
      { event: 'INSERT', schema: 'public', table: 'debug_events' },
      (payload: RealtimePostgresInsertPayload<DebugEvent>) => {
        const row = payload.new
        if (!_matchesDebugFilters(row, filters)) return
        onEvent(row)
      },
    )
    .subscribe()
  return () => {
    void client.removeChannel(channel)
  }
}

/**
 * Pure filter predicate — exported for testing.
 * Returns true when the event passes ALL active filters (absent filter = pass-all).
 * A null tenant_id on the event (pre-tenant signup failure) only passes a tenant_id
 * filter if the filter explicitly matches null, which it never does — callers clear
 * tenant_id filter to see pre-tenant events.
 */
export function _matchesDebugFilters(
  event: DebugEvent,
  filters: DebugEventFilters,
): boolean {
  if (filters.tenant_id !== undefined && event.tenant_id !== filters.tenant_id) return false
  if (filters.component && event.component !== filters.component) return false
  if (filters.severity && event.severity !== filters.severity) return false
  return true
}


// ─── TM Audit Events ────────────────────────────────────────────────────────────
// VT-516: Supabase Realtime subscription for tm_audit_log INSERTs.
// Mirrors the subscribeDebugEvents pattern immediately above.
//
// Table is public.tm_audit_log (migrations/147_vt514_tm_audit_log.sql). The
// realtime channel label is arbitrary client-side; the `table` selector MUST be
// 'tm_audit_log' or the subscription silently receives zero rows. The operator
// JWT (operator_claim=true) is gated by tm_audit_operator_select (mig 147), the
// verbatim mirror of pipeline_steps_operator_select (mig 030).
//
// JSONB columns (input/decision/reasoning_ref/action/result) are typed `unknown`
// — they carry redacted structured blobs, not a single scalar shape.

export interface TmAuditEvent {
  id: string
  created_at: string
  tenant_id: string
  run_id: string | null
  trace_id: string | null
  snapshot_id: string | null
  event_layer: 'knows' | 'gets' | 'decides' | 'does' | 'asks'
  event_kind: string
  actor: string
  summary: string | null
  input: unknown
  decision: unknown
  reasoning_ref: unknown
  action: unknown
  result: unknown
  severity: 'info' | 'warning' | 'error' | 'critical'
  status: string
  parent_audit_id: string | null
}

export interface TmAuditFilters {
  /** Exact tenant_id; absent = all tenants visible to the operator. */
  tenant_id?: string
  /** Filter to a single event_layer (knows/gets/decides/does/asks). */
  event_layer?: string
  /** Filter to a single event_kind. */
  event_kind?: string
  /** Filter to a single severity. */
  severity?: string
  /** Pin to a single run_id. */
  run_id?: string
}

export function subscribeTmAuditEvents(
  client: SupabaseClient,
  filters: TmAuditFilters,
  onEvent: (event: TmAuditEvent) => void,
): () => void {
  const channel = client
    .channel('ops:tm_audit_log')
    .on(
      'postgres_changes' as never,
      { event: 'INSERT', schema: 'public', table: 'tm_audit_log' },
      (payload: RealtimePostgresInsertPayload<TmAuditEvent>) => {
        const row = payload.new
        if (!_matchesTmAuditFilters(row, filters)) return
        onEvent(row)
      },
    )
    .subscribe()
  return () => {
    void client.removeChannel(channel)
  }
}

/**
 * Pure filter predicate — exported for testing.
 * Returns true when the event passes ALL active filters (absent = pass-all).
 * tenant_id is always pinned by the tenant page, so the feed is never unscoped.
 */
export function _matchesTmAuditFilters(
  event: TmAuditEvent,
  filters: TmAuditFilters,
): boolean {
  if (filters.tenant_id !== undefined && event.tenant_id !== filters.tenant_id) return false
  if (filters.event_layer && event.event_layer !== filters.event_layer) return false
  if (filters.event_kind && event.event_kind !== filters.event_kind) return false
  if (filters.severity && event.severity !== filters.severity) return false
  if (filters.run_id && event.run_id !== filters.run_id) return false
  return true
}
