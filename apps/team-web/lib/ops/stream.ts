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
