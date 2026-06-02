/** VT-292 — Escalations: scoping (fail-closed) + action authz + ops_audit write. */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
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

  it('returns de-identified rows (no PII, reference present)', async () => {
    const out = await fetchEscalations(
      { operatorId: 'op', role: OperatorRole.VTADMIN, assignedTenants: null },
      readClient([{ id: 'abc123de', tenant_id: 't1', kind: 'hard_limit', severity: 'high', status: 'open', opened_at: 'now' }]) as never,
    )
    expect(out).toHaveLength(1)
    expect(out[0]!.reference).toBe('REF#abc123')
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
