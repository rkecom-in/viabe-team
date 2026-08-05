/**
 * VT-733 slice A — the cost console's access rules.
 *
 * These exist because the row's premise turned out to be wrong in a way that matters: the
 * VTR-vs-VTAdmin write distinction it asked me to "surface" does not exist in the database at all
 * (both caps tables are RLS-forced with SELECT-only policies and no write policy), so slice A WIRES
 * cap writes into the web-layer role gate. That makes these rules the actual boundary, which means
 * they need tests that fail loudly rather than a comment claiming they are enforced elsewhere.
 */

import { describe, expect, it } from 'vitest'

import {
  canEditCaps,
  canEditPlatformCaps,
  capUtilisation,
  hasFullCostAccess,
  scopeCostTenantFilter,
} from '@/lib/ops/cost-access'

const VTADMIN = null
const VTR = ['tenant-a', 'tenant-b']

describe('role split', () => {
  it('treats a null assignment as VTAdmin/Fazal', () => {
    expect(hasFullCostAccess(VTADMIN)).toBe(true)
    expect(hasFullCostAccess(VTR)).toBe(false)
  })

  it('lets only VTAdmin change a cap — a VTR sees spend but cannot lift a ceiling', () => {
    expect(canEditCaps(VTADMIN)).toBe(true)
    expect(canEditCaps(VTR)).toBe(false)
    expect(canEditCaps([])).toBe(false)
  })

  it('gates the platform cap the same way', () => {
    expect(canEditPlatformCaps(VTADMIN)).toBe(true)
    expect(canEditPlatformCaps(VTR)).toBe(false)
  })
})

describe('tenant scoping (IDOR — the pattern caught twice before)', () => {
  it('gives VTAdmin everything when no filter is requested', () => {
    expect(scopeCostTenantFilter(VTADMIN, undefined)).toEqual({ tenantIds: null, denied: false })
  })

  it('lets VTAdmin narrow explicitly', () => {
    expect(scopeCostTenantFilter(VTADMIN, ['tenant-z'])).toEqual({
      tenantIds: ['tenant-z'],
      denied: false,
    })
  })

  it('defaults a VTR to its WHOLE assigned set, never to all', () => {
    expect(scopeCostTenantFilter(VTR, undefined)).toEqual({ tenantIds: VTR, denied: false })
  })

  it('lets a VTR narrow within its own set', () => {
    expect(scopeCostTenantFilter(VTR, ['tenant-a'])).toEqual({
      tenantIds: ['tenant-a'],
      denied: false,
    })
  })

  it('DENIES rather than silently narrowing when a VTR asks outside its set', () => {
    // Silent intersection would show fewer tenants than the operator believes they are seeing —
    // a console that lies quietly is worse than one that errors.
    expect(scopeCostTenantFilter(VTR, ['tenant-a', 'tenant-x'])).toEqual({
      tenantIds: [],
      denied: true,
    })
  })

  it('gives an unassigned VTR nothing — fail-closed, never all', () => {
    expect(scopeCostTenantFilter([], undefined)).toEqual({ tenantIds: [], denied: false })
  })
})

describe('cap utilisation', () => {
  it('returns null when NO ceiling is configured — the live state on dev', () => {
    // A missing cap and an unused cap look identical on a progress bar, and only one of them means
    // "this tenant can spend without limit". null forces the console to say which.
    expect(capUtilisation(12.5, null)).toBeNull()
  })

  it('returns null for a nonsensical zero/negative ceiling rather than dividing by it', () => {
    expect(capUtilisation(12.5, 0)).toBeNull()
    expect(capUtilisation(12.5, -1)).toBeNull()
  })

  it('computes the real share when a ceiling exists', () => {
    expect(capUtilisation(5, 20)).toBe(0.25)
    expect(capUtilisation(20, 20)).toBe(1)
    expect(capUtilisation(25, 20)).toBe(1.25) // over-cap is reported, not clamped
  })
})
