/** VT-291 — Fleet: health derivation + scoping (fail-closed). */

import { describe, expect, it } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import { deriveHealth, fetchFleet } from '@/lib/ops/fleet'

describe('VT-291 — deriveHealth', () => {
  it('red on any escalation or hard-limit', () => {
    expect(deriveHealth({ escalated: 1, hard_limits: 0, running: 0 })).toBe('red')
    expect(deriveHealth({ escalated: 0, hard_limits: 2, running: 5 })).toBe('red')
  })
  it('yellow when only in-flight', () => {
    expect(deriveHealth({ escalated: 0, hard_limits: 0, running: 3 })).toBe('yellow')
  })
  it('green when quiet', () => {
    expect(deriveHealth({ escalated: 0, hard_limits: 0, running: 0 })).toBe('green')
  })
})

describe('VT-291 — fetchFleet scoping', () => {
  function client(runs: { tenant_id: string; status: string }[], tenants: { id: string; business_name: string | null }[]) {
    return {
      from: (table: string) => {
        if (table === 'tenants') {
          return { select: () => ({ in: async () => ({ data: tenants }) }) }
        }
        // pipeline_runs: select().gte()[.in()]
        const builder: any = {
          select: () => builder,
          gte: () => builder,
          in: () => builder,
          then: undefined,
        }
        // make it awaitable to { data: runs }
        return {
          select: () => ({
            gte: () => ({
              in: async () => ({ data: runs }),
              then: (res: any) => res({ data: runs }),
            }),
          }),
        }
      },
    }
  }

  it('VTR with no assignments → [] (fail-closed)', async () => {
    const out = await fetchFleet({ role: OperatorRole.VTR, assignedTenants: [] }, client([], []) as never)
    expect(out).toEqual([])
  })

  it('aggregates per tenant + derives health', async () => {
    const runs = [
      { tenant_id: 'ta', status: 'running' },
      { tenant_id: 'ta', status: 'escalated' },
      { tenant_id: 'tb', status: 'running' },
    ]
    const tenants = [
      { id: 'ta', business_name: 'Asha' },
      { id: 'tb', business_name: 'Bina' },
    ]
    const out = await fetchFleet(
      { role: OperatorRole.VTR, assignedTenants: ['ta', 'tb'] },
      client(runs, tenants) as never,
    )
    const ta = out.find((r) => r.tenant_id === 'ta')!
    const tb = out.find((r) => r.tenant_id === 'tb')!
    expect(ta.health).toBe('red') // has an escalation
    expect(ta.escalated).toBe(1)
    expect(ta.running).toBe(1)
    expect(tb.health).toBe('yellow') // running only
    expect(ta.tenant_name).toBe('Asha')
  })
})
