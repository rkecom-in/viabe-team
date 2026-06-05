/** VT-338 — owner-portal i18n (EN + HI). */

import { describe, expect, it } from 'vitest'

import { getDictionary, resolveLocale, t } from '@/lib/i18n'

describe('VT-338 — i18n', () => {
  it('resolveLocale: ?lang override > tenant default > en; unknown -> en', () => {
    expect(resolveLocale('hi', 'en')).toBe('hi')
    expect(resolveLocale(null, 'hi')).toBe('hi')
    expect(resolveLocale('xx', 'yy')).toBe('en')
    expect(resolveLocale()).toBe('en')
  })

  it('t: looks up a key, falls back to the key itself when missing', () => {
    const dict = getDictionary('en')
    expect(t(dict, 'nav.customers')).toBe('Customers')
    expect(t(dict, 'no.such.key')).toBe('no.such.key')
  })

  it('hi has the SAME key set as en (no missing/extra translation)', () => {
    expect(Object.keys(getDictionary('hi')).sort()).toEqual(Object.keys(getDictionary('en')).sort())
  })
})
