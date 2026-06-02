/** VT-294 — Behaviour: metrics scope (VTR own / VTAdmin all) + train authz. */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import { fetchDecisionMetrics, recordTraining } from '@/lib/ops/behaviour'

describe('VT-294 — fetchDecisionMetrics', () => {
  function client(rows: { action: string }[], capture?: (eqArgs: any[]) => void) {
    const eqCalls: any[] = []
    const chain: any = {
      select: () => chain,
      gte: () => chain,
      eq: (...a: any[]) => { eqCalls.push(a); capture?.(eqCalls); return chain },
      then: (r: any) => r({ data: rows }),
    }
    return { from: () => chain, eqCalls }
  }

  it('VTR → own scope, filters by operator_id, counts by action', async () => {
    const c = client([{ action: 'resolve' }, { action: 'resolve' }, { action: 'override' }])
    const m = await fetchDecisionMetrics({ operatorId: 'op1', role: OperatorRole.VTR, assignedTenants: ['ta'] }, c as never)
    expect(m.scope).toBe('own')
    expect(m.total).toBe(3)
    expect(m.byAction.resolve).toBe(2)
    expect(m.byAction.override).toBe(1)
    expect(c.eqCalls[0]).toEqual(['operator_id', 'op1']) // scoped to self
  })

  it('VTAdmin → all scope, no operator filter', async () => {
    const c = client([{ action: 'resolve' }])
    const m = await fetchDecisionMetrics({ operatorId: 'admin', role: OperatorRole.VTADMIN, assignedTenants: null }, c as never)
    expect(m.scope).toBe('all')
    expect(c.eqCalls.length).toBe(0) // unscoped
  })
})

describe('VT-294 — recordTraining authz', () => {
  // client resolves the decision's TRUE owner from ops_audit; caller passes NO operator field.
  function trainClient(decisionOwner: string | null) {
    const insert = vi.fn(async (_r: Record<string, unknown>) => ({ error: null }))
    const client = {
      from: () => ({
        select: () => ({ eq: () => ({ limit: async () => ({ data: decisionOwner ? [{ operator_id: decisionOwner }] : [] }) }) }),
        insert,
      }),
    }
    return { client, insert }
  }

  it('IDOR: VTR trains a decision owned by ANOTHER operator → rejected, no write', async () => {
    const { client, insert } = trainClient('someoneElse')
    const res = await recordTraining(
      { operatorId: 'vtr1', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'dec1', 'note', client as never,
    )
    expect(res.ok).toBe(false)
    expect(insert).not.toHaveBeenCalled()
  })
  it('decision not found → rejected', async () => {
    const { client } = trainClient(null)
    const res = await recordTraining(
      { operatorId: 'vtr1', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'decX', 'n', client as never,
    )
    expect(res.ok).toBe(false)
    expect(res.reason).toBe('decision not found')
  })
  it('VTR training OWN decision → ok + ops_audit train row', async () => {
    const { client, insert } = trainClient('vtr1')
    const res = await recordTraining(
      { operatorId: 'vtr1', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'dec1', 'note', client as never,
    )
    expect(res.ok).toBe(true)
    expect(insert.mock.calls[0]![0].action).toBe('train')
  })
  it('VTAdmin training any → ok (no owner check)', async () => {
    const { client } = trainClient('anyVtr')
    const res = await recordTraining(
      { operatorId: 'admin', role: OperatorRole.VTADMIN, assignedTenants: null }, 'dec1', 'note', client as never,
    )
    expect(res.ok).toBe(true)
  })
})
