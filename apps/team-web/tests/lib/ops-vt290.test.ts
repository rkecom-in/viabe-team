/**
 * VT-290 — Ops Console V2 unit tests: role model, de-identification, assignment scoping.
 */

import { describe, expect, it } from 'vitest'

import { OperatorRole, isVtAdmin, resolveRole } from '@/lib/auth/roles'
import { canAccessTenant, resolveAssignedTenants } from '@/lib/ops/assignments'
import { hasPii, maskForVtr, referenceFor, type OpsRow } from '@/lib/ops/de-identify'

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

describe('VT-290 — de-identification (CL-426)', () => {
  const row: OpsRow = {
    id: 'a4f9d2e1-0000-0000-0000-000000000000',
    tenant_id: 't1',
    tenant_name: 'Asha Sarees',
    customer_name: 'John Doe',
    phone: '+919876500001',
    email: 'j@x.com',
    account_id: 'acct-9',
    severity: 'high',
    kind: 'aborted_hard_limit',
    time: '2026-06-02T00:00:00Z',
    status: 'open',
  }

  it('masks ALL PII for VTR, keeps operational fields + reference', () => {
    const masked = maskForVtr(row)
    expect(hasPii(masked as unknown as Record<string, unknown>)).toBe(false)
    expect(masked.reference).toBe('REF#a4f9d2')
    expect(masked.tenant_name).toBe('Asha Sarees')
    expect(masked.severity).toBe('high')
    // PII fields are simply absent on the masked shape
    expect((masked as unknown as Record<string, unknown>).phone).toBeUndefined()
    expect((masked as unknown as Record<string, unknown>).customer_name).toBeUndefined()
  })

  it('referenceFor is stable + non-PII', () => {
    expect(referenceFor('a4f9d2e1-xxxx')).toBe('REF#a4f9d2')
    expect(referenceFor('')).toBe('REF#unknown')
  })

  it('hasPii detects raw rows', () => {
    expect(hasPii(row as unknown as Record<string, unknown>)).toBe(true)
  })
})

describe('VT-290 — assignment scoping (fail-closed)', () => {
  function fakeClient(rows: { tenant_id: string }[]) {
    return {
      from: () => ({
        select: () => ({
          eq: () => ({
            is: async () => ({ data: rows, error: null }),
          }),
        }),
      }),
    }
  }

  it('VTAdmin → null (unscoped, all tenants)', async () => {
    const out = await resolveAssignedTenants('op1', OperatorRole.VTADMIN, fakeClient([]) as never)
    expect(out).toBeNull()
  })

  it('VTR → its active assigned tenants', async () => {
    const out = await resolveAssignedTenants(
      'op1', OperatorRole.VTR, fakeClient([{ tenant_id: 'ta' }, { tenant_id: 'tb' }]) as never,
    )
    expect(out).toEqual(['ta', 'tb'])
  })

  it('VTR with no operatorId → [] (fail-closed)', async () => {
    const out = await resolveAssignedTenants('', OperatorRole.VTR, fakeClient([{ tenant_id: 'ta' }]) as never)
    expect(out).toEqual([])
  })

  it('canAccessTenant: VTAdmin always; VTR iff assigned', () => {
    expect(canAccessTenant(null, 'anything')).toBe(true)
    expect(canAccessTenant(['ta', 'tb'], 'ta')).toBe(true)
    expect(canAccessTenant(['ta'], 'tz')).toBe(false)
    expect(canAccessTenant([], 'ta')).toBe(false)
  })
})
