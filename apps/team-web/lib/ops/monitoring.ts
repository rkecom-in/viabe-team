/**
 * VT-296 — Ops Console V2 Monitoring / Watchdog board (read-only).
 *
 * Fleet-wide agent health without polling each agent: reads the VT-202 watchdog detector
 * output (`tenant_alerts`, last 24h) and maps each detector's trigger_kind → a
 * crash / stall / misbehaviour category with severity, grouped per business. Same detectors
 * the VT-298 Telegram push consumes (board = the visual; VT-298 = the push).
 *
 * Scoping (VT-290 contract): VTR sees ONLY assigned tenants (fail-CLOSED empty if none);
 * VTAdmin sees all. De-identified for VTR (CL-426): the detector's message_text — though
 * already PII-scrubbed at dispatch — is dropped for the VTR view; operational fields
 * (category, kind, severity, time, run reference) remain. tenant_alerts is service-role
 * (RLS-bypassing serverSecretClient); scoping is app-side.
 *
 * Read-only: the board exposes NO mutations (no IDOR surface). Each item carries its run_id
 * so the overlay drills into a real run — never a dead end.
 */

import { serverSecretClient } from '@/lib/supabase-client'

import { OperatorRole } from '@/lib/auth/roles'
import { fetchVtrMonitoring } from '@/lib/orchestrator-client'

type Client = { from: (t: string) => any }

interface OpsOperatorLike {
  operatorId: string
  role: OperatorRole
  assignedTenants: string[] | null
}

export type DetectorCategory = 'crash' | 'stall' | 'misbehaviour'

/** Map a VT-202 trigger_kind → the VT-296 watchdog category. */
const CATEGORY_BY_KIND: Record<string, DetectorCategory> = {
  hard_limit: 'crash',
  error_envelope: 'crash',
  outbound_failure: 'crash',
  latency_anomaly: 'stall',
  escalation: 'misbehaviour',
  privacy_audit_event: 'misbehaviour',
  cost_anomaly: 'misbehaviour',
  volume_spike: 'misbehaviour',
}

export function categoryForKind(kind: string): DetectorCategory {
  return CATEGORY_BY_KIND[kind] ?? 'misbehaviour'
}

export interface MonitoringItem {
  id: string
  tenant_id: string
  tenant_name: string | null
  category: DetectorCategory
  kind: string
  severity: string
  time: string | null
  /** the offending run — the overlay drill-in target (null if the detector had none). */
  run_id: string | null
  /** stable non-PII handle (VTR view). */
  reference: string
  /** detector text (PII-scrubbed); present for VTAdmin only (null for VTR — CL-426). */
  message_text: string | null
}

const _SEVERITY_RANK: Record<string, number> = { critical: 0, warning: 1 }

/** Recent detector firings grouped into board items, scoped + de-identified. */
export async function fetchMonitoringBoard(
  operator: OpsOperatorLike,
  client: Client = serverSecretClient(),
  limit = 200,
): Promise<MonitoringItem[]> {
  const { assignedTenants, role } = operator
  if (assignedTenants !== null && assignedTenants.length === 0) return [] // fail-closed

  let items: MonitoringItem[]
  if (role !== OperatorRole.VTADMIN) {
    // VT-360: VTR → DB-enforced via the orchestrator (vtr_tenant_alerts — no message_text, and no
    // run_id drill-in per early-review F3). NO raw tenant_alerts, NO app-side masking.
    const rows = await fetchVtrMonitoring(operator.operatorId)
    items = rows.slice(0, limit).map((r) => ({
      id: String(r.alert_id),
      tenant_id: String(r.tenant_id),
      tenant_name: (r.tenant_name as string | null) ?? null,
      category: categoryForKind(String(r.trigger_kind)),
      kind: String(r.trigger_kind),
      severity: String(r.severity),
      time: (r.fired_at as string | null) ?? null,
      run_id: null, // F3: a VTR holds no run_id (no drill-in pivot)
      reference: String(r.alert_id), // operational row handle (early-review F2)
      message_text: null, // de-identified server-side (view excludes it)
    }))
  } else {
    // VTAdmin (operator full-access): service-role read — full detail (message_text + run_id).
    const since = new Date()
    since.setUTCHours(since.getUTCHours() - 24)
    let q = client
      .from('tenant_alerts')
      .select('id, tenant_id, trigger_kind, severity, fired_at, run_id, message_text')
      .gte('fired_at', since.toISOString())
      .order('fired_at', { ascending: false })
      .limit(limit)
    if (assignedTenants !== null) q = q.in('tenant_id', assignedTenants)
    const { data, error } = await q
    if (error) {
      console.error('fetchMonitoringBoard: query failed; failing closed', error)
      return []
    }
    const rows = (data ?? []) as {
      id: string
      tenant_id: string
      trigger_kind: string
      severity: string
      fired_at: string
      run_id: string | null
      message_text: string | null
    }[]
    const tenantIds = Array.from(new Set(rows.map((r) => String(r.tenant_id))))
    const names = new Map<string, string | null>()
    if (tenantIds.length > 0) {
      const { data: tdata } = await client.from('tenants').select('id, business_name').in('id', tenantIds)
      for (const t of (tdata ?? []) as { id: string; business_name: string | null }[]) {
        names.set(String(t.id), t.business_name ?? null)
      }
    }
    items = rows.map((r) => ({
      id: String(r.id),
      tenant_id: String(r.tenant_id),
      tenant_name: names.get(String(r.tenant_id)) ?? null,
      category: categoryForKind(r.trigger_kind),
      kind: r.trigger_kind,
      severity: r.severity,
      time: r.fired_at,
      run_id: r.run_id ? String(r.run_id) : null,
      reference: String(r.id), // operational row handle (referenceFor deleted)
      message_text: r.message_text ?? null,
    }))
  }

  // Critical first, then most-recent.
  items.sort((a, b) => {
    const s = (_SEVERITY_RANK[a.severity] ?? 9) - (_SEVERITY_RANK[b.severity] ?? 9)
    if (s !== 0) return s
    return (b.time ?? '').localeCompare(a.time ?? '')
  })
  return items
}
