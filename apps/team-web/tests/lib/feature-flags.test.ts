/**
 * VT-448 — signup-flow feature flags. The load-bearing invariant pinned here: with PAN identify OFF
 * (the DEFAULT — the env var unset), the entity-match step's PRIMARY identify path is MANUAL GSTIN
 * entry, NOT the PAN-entry screen. The PAN→GSTIN + DIN-KYC affordances are parked (Sandbox MCA/PAN
 * gov-unreliable, Fazal 2026-06-26); manual GSTIN is the primary identify, OTP is the only ownership.
 *
 * The exported booleans are read ONCE at module load from build-time NEXT_PUBLIC_* env, so the
 * default-OFF assertion is made against the helper (which takes the flag as a param) AND against the
 * live module booleans imported under the test's own (unset) env.
 */

import { describe, expect, it } from 'vitest'

import {
  DIN_KYC_ENABLED,
  PAN_IDENTIFY_ENABLED,
  primaryIdentifyStep,
} from '@/lib/feature-flags'

describe('VT-448 signup feature flags (default OFF)', () => {
  it('PAN_IDENTIFY_ENABLED defaults false (env var unset in the test env)', () => {
    // The vitest env doesn't set NEXT_PUBLIC_ENABLE_PAN_IDENTIFY → the parked path stays off.
    expect(process.env.NEXT_PUBLIC_ENABLE_PAN_IDENTIFY).not.toBe('true')
    expect(PAN_IDENTIFY_ENABLED).toBe(false)
  })

  it('DIN_KYC_ENABLED defaults false (env var unset in the test env)', () => {
    expect(process.env.NEXT_PUBLIC_ENABLE_DIN_KYC).not.toBe('true')
    expect(DIN_KYC_ENABLED).toBe(false)
  })
})

describe('VT-448 primaryIdentifyStep (the gated primary-path decider)', () => {
  it('with PAN identify OFF (default), the primary path is MANUAL GSTIN entry — no PAN surfaced', () => {
    expect(primaryIdentifyStep(false)).toBe('manual_gstin')
  })

  it('the live default (flag unset) resolves to manual GSTIN entry', () => {
    // No arg → reads the module-level PAN_IDENTIFY_ENABLED (false under the test env).
    expect(primaryIdentifyStep()).toBe('manual_gstin')
  })

  it('with PAN identify ON, the primary path is the PAN-entry screen (parked code intact)', () => {
    expect(primaryIdentifyStep(true)).toBe('pan_entry')
  })
})
