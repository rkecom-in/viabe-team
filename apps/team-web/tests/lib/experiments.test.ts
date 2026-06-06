/** VT-100 — cookie-free A/B: deterministic assignment, even split, IP truncation, inactive demo. */

import { describe, expect, it } from 'vitest'

import { assignVariant, findExperiment, truncateIp } from '@/lib/experiments'

describe('VT-100 truncateIp (reduce identifiability, keep consistency)', () => {
  it('IPv4 → /24 (host octet zeroed)', () => {
    expect(truncateIp('203.0.113.45')).toBe('203.0.113.0')
  })
  it('IPv6 → /48 (first 3 hextets)', () => {
    expect(truncateIp('2001:db8:abcd:1234::1')).toBe('2001:db8:abcd')
  })
  it('garbage → as-is (no crash)', () => {
    expect(truncateIp('unknown')).toBe('unknown')
  })
})

describe('VT-100 assignVariant (cookie-free, deterministic)', () => {
  const V = ['control', 'variant_b']

  it('same visitor (same /24 + UA) + same experiment → same variant', () => {
    const a = assignVariant('exp1', '203.0.113.45', 'UA/1', V)
    const b = assignVariant('exp1', '203.0.113.99', 'UA/1', V) // same /24 → same hash input
    expect(a).toBe(b)
    expect(V).toContain(a)
  })

  it('roughly even split across 1000 synthetic visitors (no cookie, hash fairness)', () => {
    let control = 0
    for (let i = 0; i < 1000; i++) {
      if (assignVariant('exp1', `10.0.${i % 256}.0`, `UA/${i}`, V) === 'control') control++
    }
    expect(control).toBeGreaterThan(380)
    expect(control).toBeLessThan(620)
  })

  it('throws on no variants (never silently mis-assign)', () => {
    expect(() => assignVariant('e', '1.2.3.4', 'ua', [])).toThrow()
  })
})

describe('VT-100 config', () => {
  it('homepage_hero_v1 ships INACTIVE (framework only — no live experiment in Phase 1)', () => {
    const e = findExperiment('homepage_hero_v1')
    expect(e).toBeDefined()
    expect(e?.active).toBe(false)
    expect(e?.variants).toEqual(['control', 'variant_b'])
  })
})
