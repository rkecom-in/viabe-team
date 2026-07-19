/**
 * VT-516 — TM Audit feed helpers.
 *
 * Pure utilities (summary composition) + server-side query helper.
 * Kept separate from stream.ts so the pure functions are testable without
 * importing the Supabase browser client. Mirrors lib/ops/debug-events.ts
 * exactly (same serverSecretClient pattern).
 *
 * Table: public.tm_audit_log (migrations/147_vt514_tm_audit_log.sql).
 */

import { serverSecretClient } from '@/lib/supabase-client'
import type { TmAuditEvent, TmAuditFilters } from './stream'

// Re-export so consumers only need one import.
export type { TmAuditEvent, TmAuditFilters }

// Column list is authoritative against migrations/147_vt514_tm_audit_log.sql.
const _TM_AUDIT_SELECT =
  'id, created_at, tenant_id, run_id, trace_id, snapshot_id, event_layer, event_kind, actor, summary, input, decision, reasoning_ref, action, result, severity, status, parent_audit_id'

const _ALLOWED_SEVERITIES = new Set<string>(['info', 'warning', 'error', 'critical'])


/**
 * Compose a single-line audit summary shown in the compact feed row.
 *
 * Format:  actor · event_layer.event_kind → summary
 *
 * Examples:
 *   "team_manager · decides.route_decided → routed to sales_recovery"
 *   "integration · gets.retrieval"  (no summary, falls back to the prefix)
 */
export function composeTmAuditSummary(
  event: Pick<TmAuditEvent, 'actor' | 'event_layer' | 'event_kind' | 'summary'>,
): string {
  const prefix = `${event.actor} · ${event.event_layer}.${event.event_kind}`
  return event.summary ? `${prefix} → ${event.summary}` : prefix
}


/**
 * Server-side read of recent tm_audit_log rows (newest first, capped at `limit`).
 * Callers MUST have already verified operator auth before calling this.
 * Optional filters: tenant_id, event_layer, event_kind, severity, run_id.
 */
export async function fetchRecentTmAuditEvents(
  filters: {
    tenant_id?: string
    event_layer?: string
    event_kind?: string
    severity?: string
    run_id?: string
  } = {},
  limit = 100,
): Promise<TmAuditEvent[]> {
  const client = serverSecretClient()

  // Dynamic chaining: each .eq() narrows the type differently; use any to avoid
  // excessively-deep instantiation (same pattern as debug-events.ts).
  // biome-ignore lint/suspicious/noExplicitAny: supabase builder chaining
  let query: any = client
    .from('tm_audit_log')
    .select(_TM_AUDIT_SELECT)
    .order('created_at', { ascending: false })
    .limit(limit)

  if (filters.tenant_id) {
    query = query.eq('tenant_id', filters.tenant_id)
  }
  if (filters.event_layer) {
    query = query.eq('event_layer', filters.event_layer)
  }
  if (filters.event_kind) {
    query = query.eq('event_kind', filters.event_kind)
  }
  if (filters.severity && _ALLOWED_SEVERITIES.has(filters.severity)) {
    query = query.eq('severity', filters.severity)
  }
  if (filters.run_id) {
    query = query.eq('run_id', filters.run_id)
  }

  const { data, error } = await query as { data: unknown[] | null; error: { message: string } | null }
  if (error) {
    throw new Error(`fetchRecentTmAuditEvents: ${error.message}`)
  }
  return (data ?? []) as TmAuditEvent[]
}
