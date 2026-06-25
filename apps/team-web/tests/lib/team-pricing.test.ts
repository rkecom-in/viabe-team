/** VT-429 — single-plan launch gate (owner-facing): planPrices() presents ONLY the offered tiers
 * from NEXT_PUBLIC_OFFERED_TIERS, fail-closed (empty/absent → no cards, never offer-all). The
 * load-bearing gate is server-side; this is the presentation mirror. */

import { afterEach, describe, expect, it } from 'vitest'

import { offeredTiers, planPrices } from '@/lib/team-pricing'

const ORIG = process.env.NEXT_PUBLIC_OFFERED_TIERS

afterEach(() => {
  if (ORIG === undefined) delete process.env.NEXT_PUBLIC_OFFERED_TIERS
  else process.env.NEXT_PUBLIC_OFFERED_TIERS = ORIG
})

describe('VT-429 offeredTiers', () => {
  it('parses the comma-separated env into the known-tier set', () => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'standard'
    expect(offeredTiers()).toEqual(new Set(['standard']))
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'standard, pro'
    expect(offeredTiers()).toEqual(new Set(['standard', 'pro']))
  })

  it('FAIL-CLOSED: an absent env offers NOTHING (empty set), never offer-all', () => {
    delete process.env.NEXT_PUBLIC_OFFERED_TIERS
    expect(offeredTiers()).toEqual(new Set())
  })

  it('FAIL-CLOSED: a blank / whitespace-only env offers NOTHING', () => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = '   '
    expect(offeredTiers()).toEqual(new Set())
    process.env.NEXT_PUBLIC_OFFERED_TIERS = ''
    expect(offeredTiers()).toEqual(new Set())
  })

  it('ignores unknown tokens (an unknown env value cannot conjure a tier)', () => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'enterprise, standard'
    expect(offeredTiers()).toEqual(new Set(['standard']))
  })
})

describe('VT-429 planPrices (the plan-selection surface)', () => {
  it('renders ONLY Standard at launch (founding + pro hidden)', () => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'standard'
    const tiers = planPrices().map((p) => p.tier)
    expect(tiers).toEqual(['standard'])
    expect(tiers).not.toContain('founding')
    expect(tiers).not.toContain('pro')
  })

  it('FAIL-CLOSED: an empty offered set renders NO cards (not all plans)', () => {
    delete process.env.NEXT_PUBLIC_OFFERED_TIERS
    expect(planPrices()).toEqual([])
  })

  it('widening to multiple tiers presents them in canonical order', () => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'pro, founding, standard'
    expect(planPrices().map((p) => p.tier)).toEqual(['founding', 'standard', 'pro'])
  })

  it('a presented tier still carries its display price', () => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'standard'
    const standard = planPrices().find((p) => p.tier === 'standard')
    expect(standard?.inr).toBeTruthy()
  })
})
