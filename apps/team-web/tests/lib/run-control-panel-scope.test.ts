/**
 * VT-377 panel leg (build contract §B3.5) — the resolveAssignedTenants intersection the
 * run-control page applies before rendering tenant tiles: a VTR session renders ONLY its
 * assigned tiles; a VTAdmin session renders ALL. Tested as a pure fn (scopeTenantsForOperator
 * — extracted to its own dep-less module) PLUS the session→scope wiring through
 * resolveAssignedTenants (the same role→assigned-set resolver the page's requireOpsOperator
 * uses), mocked per the existing ops-vt290 fakeFactory idiom. No component harness invented
 * (the established ops-page constraint).
 */

import { describe, expect, it } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import { resolveAssignedTenants } from '@/lib/ops/assignments'
import {
  scopeTenantsForOperator,
  type ScopeTenant,
} from '@/app/(app)/team/ops/run-control/scope-tenants'

const TENANTS: ScopeTenant[] = [
  { tenant_id: 'ta', business_name: 'Alpha' },
  { tenant_id: 'tb', business_name: 'Bravo' },
  { tenant_id: 'tc', business_name: 'Charlie' },
]

// VT-380 idiom: third arg is a clientFactory (() => Client), resolved lazily.
function fakeFactory(rows: { tenant_id: string }[]) {
  return () => ({
    from: () => ({
      select: () => ({
        eq: () => ({
          is: async () => ({ data: rows, error: null }),
        }),
      }),
    }),
  })
}

describe('VT-377 — scopeTenantsForOperator intersection', () => {
  it('VTAdmin (assignedTenants null) → ALL tiles, unscoped', () => {
    expect(scopeTenantsForOperator(TENANTS, null)).toEqual(TENANTS)
  })

  it('VTR → ONLY the intersection with its assigned set (assigned tiles only)', () => {
    const out = scopeTenantsForOperator(TENANTS, ['tb'])
    expect(out).toHaveLength(1)
    expect(out[0]!.tenant_id).toBe('tb')
  })

  it('VTR → intersection drops assigned ids NOT in the tenant list (no phantom tiles)', () => {
    const out = scopeTenantsForOperator(TENANTS, ['tb', 'tz-not-listed'])
    expect(out.map((t) => t.tenant_id)).toEqual(['tb'])
  })

  it('VTR with an EMPTY assigned set → NO tiles (fail-closed)', () => {
    expect(scopeTenantsForOperator(TENANTS, [])).toEqual([])
  })

  it('preserves the original tile order + shape on the VTR path', () => {
    const out = scopeTenantsForOperator(TENANTS, ['tc', 'ta'])
    expect(out.map((t) => t.tenant_id)).toEqual(['ta', 'tc']) // input order, not assigned order
    expect(out[0]!.business_name).toBe('Alpha')
  })
})

describe('VT-377 — session → scope wiring (resolveAssignedTenants drives the intersection)', () => {
  it('VTAdmin session → null set → ALL tiles render', async () => {
    const assigned = await resolveAssignedTenants('op-admin', OperatorRole.VTADMIN, fakeFactory([]))
    expect(assigned).toBeNull()
    expect(scopeTenantsForOperator(TENANTS, assigned)).toEqual(TENANTS)
  })

  it('VTR session → its assigned set → ONLY those tiles render', async () => {
    const assigned = await resolveAssignedTenants(
      'op-vtr',
      OperatorRole.VTR,
      fakeFactory([{ tenant_id: 'ta' }, { tenant_id: 'tc' }]),
    )
    expect(assigned).toEqual(['ta', 'tc'])
    const tiles = scopeTenantsForOperator(TENANTS, assigned)
    expect(tiles.map((t) => t.tenant_id)).toEqual(['ta', 'tc'])
    // and never the unassigned tenant.
    expect(tiles.some((t) => t.tenant_id === 'tb')).toBe(false)
  })

  it('VTR session with no assignments → [] → NO tiles render (fail-closed)', async () => {
    const assigned = await resolveAssignedTenants('op-vtr', OperatorRole.VTR, fakeFactory([]))
    expect(assigned).toEqual([])
    expect(scopeTenantsForOperator(TENANTS, assigned)).toEqual([])
  })
})
