/** VT-297 — Telegram mutating actions: IDOR-safe (resolve within the operator's scoped set). */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'

// VT-360: the VTR read path goes through the orchestrator (app_vtr_role views), not the client.
vi.mock('@/lib/orchestrator-client', () => ({
  fetchVtrEscalations: vi.fn(),
  fetchVtrMonitoring: vi.fn(),
}))
import { fetchVtrEscalations } from '@/lib/orchestrator-client'
import { actByReference } from '@/lib/telegram/actions'

const VTR = (assigned: string[]) => ({ operatorId: 'op', role: OperatorRole.VTR, assignedTenants: assigned })

/** Client capturing escalations.update + ops_audit.insert (the WRITE; the READ is the mocked endpoint). */
function client() {
  const audit: any[] = []
  const updates: any[] = []
  const c = {
    from: (table: string) => {
      if (table === 'ops_audit') return { insert: async (row: any) => (audit.push(row), { error: null }) }
      // escalations.update().eq()
      return { update: (patch: any) => ({ eq: async (_c: string, id: string) => (updates.push({ patch, id }), { error: null }) }) }
    },
  }
  return { c, audit, updates }
}

/** Shape an orchestrator vtr_escalations row (what fetchVtrEscalations returns). */
const vtrRow = (escalation_id: string, tenant_id: string) => ({
  escalation_id, tenant_id, tenant_name: 'Asha', kind: 'hard_limit',
  severity: 'high', status: 'open', opened_at: 'now', route: 'vtr',
})

describe('VT-297 — actByReference (IDOR-safe; VT-360 UUID handle)', () => {
  it('acts on an id that IS in the operator scoped set → update + audit with server-resolved tenant', async () => {
    vi.mocked(fetchVtrEscalations).mockResolvedValue([vtrRow('esc-aaaa-1111', 't-assigned')])
    const { c, audit, updates } = client()
    const out = await actByReference(VTR(['t-assigned']), 'esc-aaaa-1111', 'ack', c as never)
    expect(out).toContain('Acknowledged')
    expect(updates).toHaveLength(1)
    // tenant resolved FROM the scoped row, not a chat field.
    expect(audit[0]).toMatchObject({ action: 'ack', target_kind: 'escalation', tenant_id: 't-assigned', operator_id: 'op' })
  })

  it('id NOT in the operator scoped set → no action, no audit (foreign escalation unreachable)', async () => {
    vi.mocked(fetchVtrEscalations).mockResolvedValue([]) // scoped read returns none
    const { c, audit, updates } = client()
    const out = await actByReference(VTR(['t-assigned']), 'esc-ffff-9999', 'resolve', c as never)
    expect(out).toContain('No open escalation')
    expect(updates).toHaveLength(0)
    expect(audit).toHaveLength(0)
  })

  it('exact-match only: a prefix of an id does NOT match (no collision/ambiguity)', async () => {
    vi.mocked(fetchVtrEscalations).mockResolvedValue([vtrRow('esc-aaaa-1111', 't1')])
    const { c, updates } = client()
    const out = await actByReference(VTR(['t1']), 'esc-aaaa', 'ack', c as never) // prefix, not the full id
    expect(out).toContain('No open escalation')
    expect(updates).toHaveLength(0)
  })

  it('VTR with no assignments → fail-closed (empty scoped set) → no action', async () => {
    const { c, updates } = client()
    // fetchEscalations short-circuits to [] for assignedTenants=[] BEFORE any read.
    const out = await actByReference(VTR([]), 'esc-aaaa-1111', 'ack', c as never)
    expect(out).toContain('No open escalation')
    expect(updates).toHaveLength(0)
  })

  it('empty reference → usage hint', async () => {
    const { c } = client()
    const out = await actByReference(VTR(['t1']), '', 'ack', c as never)
    expect(out).toContain('Usage')
  })
})
