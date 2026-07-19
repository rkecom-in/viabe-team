/**
 * VT-290 — Ops Console V2 unit tests: role model, de-identification, assignment scoping.
 */

import { describe, expect, it } from 'vitest'

import { OperatorRole, isVtAdmin, resolveRole } from '@/lib/auth/roles'
import { canAccessTenant, resolveAssignedTenants } from '@/lib/ops/assignments'

describe('VT-290 — roles', () => {
  it('Fazal → VTAdmin (break-glass)', () => {
    expect(resolveRole(undefined, { isFazal: true })).toBe(OperatorRole.VTADMIN)
  })
  it('explicit vt_admin → VTAdmin', () => {
    expect(resolveRole('vt_admin')).toBe(OperatorRole.VTADMIN)
  })
  it('unknown/empty → VTR (least privilege, fail-closed)', () => {
    expect(resolveRole(undefined)).toBe(OperatorRole.VTR)
    expect(resolveRole('nonsense')).toBe(OperatorRole.VTR)
  })
  it('isVtAdmin', () => {
    expect(isVtAdmin(OperatorRole.VTADMIN)).toBe(true)
    expect(isVtAdmin(OperatorRole.VTR)).toBe(false)
  })
})

// VT-360: the app-side de-identification (maskForVtr/referenceFor/hasPii) was RETIRED — VTR
// de-identification is now DB-ENFORCED via the orchestrator (app_vtr_role + the VT-281/360 views).
// Those unit tests are replaced by the orchestrator-side canary (test_vtr_reads.py — denied
// raw-table reads + view-columns-only) and the role-split tests in ops-escalations / ops-monitoring.

describe('VT-290 — assignment scoping (fail-closed)', () => {
  // VT-380 (B3): third arg is now a clientFactory (() => Client), not a Client instance.
  // serverSecretClient() is resolved LAZILY — never called before the VTADMIN/empty-id
  // early-returns, so a credless env never throws for VTAdmin callers.
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

  it('VTAdmin → null (unscoped, all tenants)', async () => {
    const out = await resolveAssignedTenants('op1', OperatorRole.VTADMIN, fakeFactory([]))
    expect(out).toBeNull()
  })

  it('VTR → its active assigned tenants', async () => {
    const out = await resolveAssignedTenants(
      'op1', OperatorRole.VTR, fakeFactory([{ tenant_id: 'ta' }, { tenant_id: 'tb' }]),
    )
    expect(out).toEqual(['ta', 'tb'])
  })

  it('VTR with no operatorId → [] (fail-closed)', async () => {
    const out = await resolveAssignedTenants('', OperatorRole.VTR, fakeFactory([{ tenant_id: 'ta' }]))
    expect(out).toEqual([])
  })

  it('VTAdmin + credless env (factory throws) → null WITHOUT calling factory (lazy)', async () => {
    // The factory must NEVER be invoked for VTADMIN — the early-return fires before it.
    let factoryCalled = false
    const throwingFactory = () => {
      factoryCalled = true
      throw new Error('serverSecretClient: SUPABASE_URL and SUPABASE_SECRET_KEY must be set')
    }
    const out = await resolveAssignedTenants('op1', OperatorRole.VTADMIN, throwingFactory)
    expect(out).toBeNull()
    expect(factoryCalled).toBe(false)
  })

  it('VTR + credless env (factory throws) → [] (degrade, not throw)', async () => {
    // When credentials are missing, resolveAssignedTenants degrades to fail-closed
    // rather than propagating a hard 500.
    const throwingFactory = () => {
      throw new Error('serverSecretClient: SUPABASE_URL and SUPABASE_SECRET_KEY must be set')
    }
    const out = await resolveAssignedTenants('op1', OperatorRole.VTR, throwingFactory)
    expect(out).toEqual([])
  })

  it('canAccessTenant: VTAdmin always; VTR iff assigned', () => {
    expect(canAccessTenant(null, 'anything')).toBe(true)
    expect(canAccessTenant(['ta', 'tb'], 'ta')).toBe(true)
    expect(canAccessTenant(['ta'], 'tz')).toBe(false)
    expect(canAccessTenant([], 'ta')).toBe(false)
  })
})
