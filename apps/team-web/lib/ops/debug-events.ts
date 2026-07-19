/**
 * VT-515 — Debug/Failures feed helpers.
 *
 * Pure utilities (summary composition) + server-side query helper.
 * Kept separate from stream.ts so the pure functions are testable
 * without importing the Supabase browser client.
 */

import { serverSecretClient } from '@/lib/supabase-client'
import type { DebugEvent, DebugEventFilters } from './stream'

// Re-export the event type so consumers only need one import.
export type { DebugEvent, DebugEventFilters }

const _DEBUG_SELECT =
  'id, created_at, tenant_id, trace_id, failure_type, component, operation, error_message, error_stack, context, severity, impact, vendor, vendor_status, latency_ms'

const _ALLOWED_SEVERITIES = new Set<string>(['warning', 'error', 'critical'])


/**
 * Compose a single-line event summary shown in the compact feed row.
 *
 * Format:  component · operation · failure_type → impact
 *
 * Examples:
 *   "discovery · knowyourgst · timeout → degraded_to_manual"
 *   "create · invalid_gstin · validation → blocked_signup"
 *   "send · vendor_error: STALE_TEMPLATE"   (no impact, falls back to error_message)
 */
export function composeEventSummary(
  event: Pick<DebugEvent, 'component' | 'operation' | 'failure_type' | 'impact' | 'error_message'>,
): string {
  const parts: string[] = [event.component]
  if (event.operation) parts.push(event.operation)
  // failure_type is always set per schema; include as human-readable label
  parts.push(event.failure_type.replace(/_/g, ' '))
  const prefix = parts.join(' · ')
  if (event.impact) return `${prefix} → ${event.impact}`
  if (event.error_message) {
    // Truncate long error messages in the summary line; full text in drill-down.
    const msg = event.error_message.slice(0, 80)
    return `${prefix}: ${msg}`
  }
  return prefix
}


/**
 * Server-side read of recent debug_events (newest first, capped at `limit`).
 * Callers MUST have already verified operator auth before calling this.
 * Optional filters: tenant_id, component, severity.
 */
export async function fetchRecentDebugEvents(
  filters: { tenant_id?: string; component?: string; severity?: string } = {},
  limit = 100,
): Promise<DebugEvent[]> {
  const client = serverSecretClient()

  // Dynamic chaining: each .eq() narrows the type differently; use any to avoid
  // excessively-deep instantiation (same pattern as data-access.ts callers).
  // biome-ignore lint/suspicious/noExplicitAny: supabase builder chaining
  let query: any = client
    .from('debug_events')
    .select(_DEBUG_SELECT)
    .order('created_at', { ascending: false })
    .limit(limit)

  if (filters.tenant_id) {
    query = query.eq('tenant_id', filters.tenant_id)
  }
  if (filters.component) {
    query = query.eq('component', filters.component)
  }
  if (filters.severity && _ALLOWED_SEVERITIES.has(filters.severity)) {
    query = query.eq('severity', filters.severity)
  }

  const { data, error } = await query as { data: unknown[] | null; error: { message: string } | null }
  if (error) {
    throw new Error(`fetchRecentDebugEvents: ${error.message}`)
  }
  return (data ?? []) as DebugEvent[]
}
