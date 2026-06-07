/** VT-296 — Monitoring board: scoping (fail-closed), category mapping, CL-426 de-id, sort. */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'

// VT-360: the VTR read path goes through the orchestrator (app_vtr_role views), not the client.
vi.mock('@/lib/orchestrator-client', () => ({
  fetchVtrEscalations: vi.fn(),
  fetchVtrMonitoring: vi.fn(),
}))
import { fetchVtrMonitoring } from '@/lib/orchestrator-client'
import { categoryForKind, fetchMonitoringBoard } from '@/lib/ops/monitoring'

/** Programmable client: tenant_alerts list (thenable) + tenants name lookup (thenable). */
function client(alerts: any[], tenants: any[] = []) {
  const from = (table: string) => {
    const builder: any = {
      select: () => builder,
      gte: () => builder,
      order: () => builder,
      limit: () => builder,
      in: () => builder,
      then: (resolve: any) =>
        resolve(table === 'tenants' ? { data: tenants } : { data: alerts, error: null }),
    }
    return builder
  }
  return { from }
}

const ADMIN = { operatorId: 'admin', role: OperatorRole.VTADMIN, assignedTenants: null }
const VTR = (assigned: string[]) => ({ operatorId: 'vtr', role: OperatorRole.VTR, assignedTenants: assigned })

describe('VT-296 — categoryForKind', () => {
  it('maps detectors to crash/stall/misbehaviour', () => {
    expect(categoryForKind('hard_limit')).toBe('crash')
    expect(categoryForKind('error_envelope')).toBe('crash')
    expect(categoryForKind('outbound_failure')).toBe('crash')
    expect(categoryForKind('latency_anomaly')).toBe('stall')
    expect(categoryForKind('escalation')).toBe('misbehaviour')
    expect(categoryForKind('privacy_audit_event')).toBe('misbehaviour')
    expect(categoryForKind('unknown_kind')).toBe('misbehaviour') // safe default
  })
})

describe('VT-296 — fetchMonitoringBoard scoping', () => {
  it('VTR with no assignments → [] (fail-closed)', async () => {
    const out = await fetchMonitoringBoard(
      VTR([]),
      client([{ id: 'a1', tenant_id: 't1', trigger_kind: 'hard_limit', severity: 'critical', fired_at: 'now' }]) as never,
    )
    expect(out).toEqual([])
  })

  it('VTAdmin → board items with category + tenant name', async () => {
    const out = await fetchMonitoringBoard(
      ADMIN,
      client(
        [{ id: 'a1', tenant_id: 't1', trigger_kind: 'latency_anomaly', severity: 'warning', fired_at: 'now', run_id: 'r1', message_text: 'scrubbed detail' }],
        [{ id: 't1', business_name: 'Asha' }],
      ) as never,
    )
    expect(out).toHaveLength(1)
    expect(out[0]!.category).toBe('stall')
    expect(out[0]!.tenant_name).toBe('Asha')
    expect(out[0]!.run_id).toBe('r1')
    expect(out[0]!.message_text).toBe('scrubbed detail') // VTAdmin sees it
  })
})

describe('VT-296 — CL-426 de-identification (VT-360: DB-enforced via the orchestrator)', () => {
  it('VTR reads the de-identified view (no message_text, no run_id), NOT the service-role client', async () => {
    // the view excludes message_text + run_id server-side (mig 118 / early-review F3).
    vi.mocked(fetchVtrMonitoring).mockResolvedValue([
      { alert_id: 'a-9', tenant_id: 't1', tenant_name: 'Asha', trigger_kind: 'escalation',
        severity: 'critical', fired_at: 'now' },
    ])
    // a client that THROWS if touched — proves the VTR path never reads raw tenant_alerts.
    const trap = { from: () => { throw new Error('VTR must not touch the service-role client') } }
    const out = await fetchMonitoringBoard(VTR(['t1']), trap as never)
    expect(fetchVtrMonitoring).toHaveBeenCalledWith('vtr')
    expect(out).toHaveLength(1)
    expect(out[0]!.message_text).toBeNull() // not in the view
    expect(out[0]!.run_id).toBeNull() // F3: no drill-in pivot for the VTR
    expect(out[0]!.reference).toBe('a-9') // operational row handle
    expect(out[0]!.category).toBe('misbehaviour')
    expect(out[0]!.tenant_name).toBe('Asha')
  })
})

describe('VT-296 — severity sort (critical first)', () => {
  it('orders critical before warning', async () => {
    const out = await fetchMonitoringBoard(
      ADMIN,
      client(
        [
          { id: 'w1', tenant_id: 't1', trigger_kind: 'cost_anomaly', severity: 'warning', fired_at: '2026-06-03T00:00:00Z', run_id: null, message_text: null },
          { id: 'c1', tenant_id: 't1', trigger_kind: 'hard_limit', severity: 'critical', fired_at: '2026-06-02T00:00:00Z', run_id: 'r1', message_text: null },
        ],
        [{ id: 't1', business_name: 'Asha' }],
      ) as never,
    )
    expect(out.map((i) => i.severity)).toEqual(['critical', 'warning'])
  })
})
