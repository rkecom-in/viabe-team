/** VT-292 — Escalations: scoping (fail-closed) + action authz + ops_audit write. */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'

// VT-360: the VTR read path goes through the orchestrator (app_vtr_role views), not the client.
vi.mock('@/lib/orchestrator-client', () => ({
  fetchVtrEscalations: vi.fn(),
  fetchVtrMonitoring: vi.fn(),
}))
import { fetchVtrEscalations } from '@/lib/orchestrator-client'
import { actOnEscalation, fetchEscalations } from '@/lib/ops/escalations'

function readClient(rows: any[]) {
  const chain: any = {
    select: () => chain,
    neq: () => chain,
    order: () => chain,
    limit: () => chain,
    in: () => chain,
    then: (resolve: any) => resolve({ data: rows }),
  }
  return { from: () => chain }
}

describe('VT-292 — fetchEscalations scoping', () => {
  it('VTR with no assignments → [] (fail-closed)', async () => {
    const out = await fetchEscalations(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: [] },
      readClient([{ id: 'e1', tenant_id: 't1' }]) as never,
    )
    expect(out).toEqual([])
  })

  it('VTAdmin: service-role read, operational fields + row-handle reference (no PII)', async () => {
    const out = await fetchEscalations(
      { operatorId: 'op', role: OperatorRole.VTADMIN, assignedTenants: null },
      readClient([{ id: 'abc123de', tenant_id: 't1', kind: 'hard_limit', severity: 'high', status: 'open', opened_at: 'now' }]) as never,
    )
    expect(out).toHaveLength(1)
    expect(out[0]!.reference).toBe('abc123de') // operational row handle (referenceFor retired)
    expect(out[0]!.kind).toBe('hard_limit')
    expect((out[0]! as unknown as Record<string, unknown>).phone).toBeUndefined()
  })

  it('VTR: reads the orchestrator de-identified view, NOT the service-role client', async () => {
    vi.mocked(fetchVtrEscalations).mockResolvedValue([
      { escalation_id: 'esc-9', tenant_id: 't1', tenant_name: 'Asha', kind: 'how_to_gap',
        severity: 'medium', status: 'open', opened_at: 'now', route: 'vtr' },
    ])
    // a client that would THROW if touched — proves the VTR path never reads raw via service-role.
    const trap = { from: () => { throw new Error('VTR must not touch the service-role client') } }
    const out = await fetchEscalations(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: null },
      trap as never,
    )
    expect(fetchVtrEscalations).toHaveBeenCalledWith('op')
    expect(out).toHaveLength(1)
    expect(out[0]!.reference).toBe('esc-9')
    expect(out[0]!.tenant_name).toBe('Asha')
    expect((out[0]! as unknown as Record<string, unknown>).phone).toBeUndefined()
  })
})

describe('VT-292 — actOnEscalation', () => {
  function actionClient() {
    const auditInsert = vi.fn(async (_row: Record<string, unknown>) => ({ error: null }))
    const client = {
      from: (table: string) => {
        if (table === 'ops_audit') return { insert: auditInsert }
        // escalations.update().eq()
        return { update: () => ({ eq: async () => ({ error: null }) }) }
      },
    }
    return { client, auditInsert }
  }

  it('VTR not assigned to tenant → rejected, no write', async () => {
    const { client, auditInsert } = actionClient()
    const res = await actOnEscalation(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] },
      'e1', 'tz', 'resolve', null, client as never,
    )
    expect(res.ok).toBe(false)
    expect(auditInsert).not.toHaveBeenCalled()
  })

  it('resolve on assigned tenant → ok + ops_audit appended', async () => {
    const { client, auditInsert } = actionClient()
    const res = await actOnEscalation(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] },
      'e1', 'ta', 'resolve', 'looks handled', client as never,
    )
    expect(res.ok).toBe(true)
    expect(auditInsert).toHaveBeenCalledTimes(1)
    const arg = auditInsert.mock.calls[0]![0]
    expect(arg.action).toBe('resolve')
    expect(arg.target_kind).toBe('escalation')
    expect(arg.operator_id).toBe('op')
  })

  it('VTAdmin (unscoped) may act on any tenant', async () => {
    const { client } = actionClient()
    const res = await actOnEscalation(
      { operatorId: 'admin', role: OperatorRole.VTADMIN, assignedTenants: null },
      'e1', 'tz', 'ack', null, client as never,
    )
    expect(res.ok).toBe(true)
  })
})
