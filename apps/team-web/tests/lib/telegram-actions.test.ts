/** VT-297 — Telegram mutating actions: IDOR-safe (resolve within the operator's scoped set). */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import { actByReference } from '@/lib/telegram/actions'

const VTR = (assigned: string[]) => ({ operatorId: 'op', role: OperatorRole.VTR, assignedTenants: assigned })

/** Client feeding fetchEscalations (scoped read) + capturing escalations.update + ops_audit.insert. */
function client(escalationRows: any[]) {
  const audit: any[] = []
  const updates: any[] = []
  const readChain: any = {
    select: () => readChain,
    neq: () => readChain,
    order: () => readChain,
    limit: () => readChain,
    in: () => readChain,
    then: (resolve: any) => resolve({ data: escalationRows, error: null }),
  }
  const c = {
    from: (table: string) => {
      if (table === 'ops_audit') return { insert: async (row: any) => (audit.push(row), { error: null }) }
      if (table === 'escalations') {
        // fetchEscalations uses select()....; actOnEscalation uses update().eq()
        return {
          ...readChain,
          update: (patch: any) => ({ eq: async (_c: string, id: string) => (updates.push({ patch, id }), { error: null }) }),
        }
      }
      return readChain
    },
  }
  return { c, audit, updates }
}

describe('VT-297 — actByReference (IDOR-safe)', () => {
  it('acts on a ref that IS in the operator scoped set → update + audit with server-resolved tenant', async () => {
    const { c, audit, updates } = client([
      { id: 'abc123de-0000', tenant_id: 't-assigned', kind: 'hard_limit', severity: 'high', status: 'open', opened_at: 'now' },
    ])
    const out = await actByReference(VTR(['t-assigned']), 'REF#abc123', 'ack', c as never)
    expect(out).toContain('Acknowledged')
    expect(updates).toHaveLength(1)
    // audit carries the tenant resolved FROM the scoped row, not a chat field
    expect(audit[0]).toMatchObject({ action: 'ack', target_kind: 'escalation', tenant_id: 't-assigned', operator_id: 'op' })
  })

  it('ref NOT in the operator scoped set → no action, no audit (foreign escalation unreachable)', async () => {
    // fetchEscalations already filtered to assigned tenants → the foreign row never appears here.
    const { c, audit, updates } = client([]) // VTR sees none
    const out = await actByReference(VTR(['t-assigned']), 'REF#ffffff', 'resolve', c as never)
    expect(out).toContain('No open escalation')
    expect(updates).toHaveLength(0)
    expect(audit).toHaveLength(0)
  })

  it('VTR with no assignments → fail-closed (empty scoped set) → no action', async () => {
    const { c, updates } = client([{ id: 'x', tenant_id: 't1', status: 'open', opened_at: 'now' }])
    // fetchEscalations returns [] for assignedTenants=[] regardless of rows
    const out = await actByReference(VTR([]), 'REF#abc123', 'ack', c as never)
    expect(out).toContain('No open escalation')
    expect(updates).toHaveLength(0)
  })

  it('empty reference → usage hint', async () => {
    const { c } = client([])
    const out = await actByReference(VTR(['t1']), '', 'ack', c as never)
    expect(out).toContain('Usage')
  })

  it('ambiguous ref (multiple matches) → refuses, points to web', async () => {
    const { c, updates } = client([
      { id: 'abc123aa-0000', tenant_id: 't1', status: 'open', opened_at: 'now' },
      { id: 'abc123bb-0000', tenant_id: 't1', status: 'open', opened_at: 'now' },
    ])
    // both map to REF#abc123
    const out = await actByReference(VTR(['t1']), 'REF#abc123', 'resolve', c as never)
    expect(out).toContain('ambiguous')
    expect(updates).toHaveLength(0)
    void vi
  })
})
