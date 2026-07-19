/** VT-95 — landing-page i18n (EN+HI parity + sections resolve) + config-sourced prices. */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { getLandingDictionary, t } from '@/lib/i18n'
import { planPrices } from '@/lib/team-pricing'

const SECTIONS = [
  'brand',
  'hero.title',
  'hero.subtitle',
  'hero.cta',
  'value.title',
  'value.1.title',
  'value.3.body',
  'pricing.title',
  'pricing.founding.name',
  'pricing.period',
  'pricing.feature.day39',
  'day39.title',
  'day39.body',
  'faq.title',
  'footer.privacy',
  'footer.rights',
]

describe('VT-95 landing i18n', () => {
  it('en + hi landing dicts have IDENTICAL keys (parity)', () => {
    const en = Object.keys(getLandingDictionary('en')).sort()
    const hi = Object.keys(getLandingDictionary('hi')).sort()
    expect(hi).toEqual(en)
  })

  it('every section resolves in BOTH locales (not the key-fallback)', () => {
    for (const loc of ['en', 'hi'] as const) {
      const d = getLandingDictionary(loc)
      for (const k of SECTIONS) {
        expect(t(d, k), `${loc}:${k}`).not.toBe(k)
        expect(t(d, k).length).toBeGreaterThan(0)
      }
    }
  })

  it('FAQ has 8 questions + answers in both locales', () => {
    for (const loc of ['en', 'hi'] as const) {
      const d = getLandingDictionary(loc)
      for (let i = 1; i <= 8; i++) {
        expect(t(d, `faq.q${i}`)).not.toBe(`faq.q${i}`)
        expect(t(d, `faq.a${i}`)).not.toBe(`faq.a${i}`)
      }
    }
  })
})

describe('VT-95 pricing (config-sourced, Pillar 7)', () => {
  // VT-429: planPrices() now presents only the OFFERED tiers (NEXT_PUBLIC_OFFERED_TIERS); these
  // VT-95 price-mapping assertions widen the offered set so they test what they mean to (config-
  // sourced rupee values, no paise literals) — the offered-filter itself is in team-pricing.test.ts.
  const ORIG_OFFERED = process.env.NEXT_PUBLIC_OFFERED_TIERS
  beforeEach(() => {
    process.env.NEXT_PUBLIC_OFFERED_TIERS = 'founding,standard,pro'
  })
  afterEach(() => {
    if (ORIG_OFFERED === undefined) delete process.env.NEXT_PUBLIC_OFFERED_TIERS
    else process.env.NEXT_PUBLIC_OFFERED_TIERS = ORIG_OFFERED
  })

  it('planPrices returns the offered tiers in canonical order', () => {
    expect(planPrices().map((p) => p.tier)).toEqual(['founding', 'standard', 'pro'])
  })

  it('prices are rupee display values, NEVER a paise literal', () => {
    for (const p of planPrices()) {
      expect(p.inr).toBeTruthy()
      expect(['249900', '499900', '1499900']).not.toContain(p.inr.replace(/,/g, ''))
    }
  })

  it('reads NEXT_PUBLIC_*_PRICE_INR when set (config override)', async () => {
    const orig = process.env.NEXT_PUBLIC_FOUNDING_PRICE_INR
    process.env.NEXT_PUBLIC_FOUNDING_PRICE_INR = '1,234'
    // re-import so the env is read fresh (planPrices reads process.env at call time)
    const { planPrices: pp } = await import('@/lib/team-pricing')
    expect(pp().find((p) => p.tier === 'founding')?.inr).toBe('1,234')
    if (orig === undefined) delete process.env.NEXT_PUBLIC_FOUNDING_PRICE_INR
    else process.env.NEXT_PUBLIC_FOUNDING_PRICE_INR = orig
  })
})
