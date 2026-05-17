import { describe, expect, it } from 'vitest'
import { scanRepo, scanText } from './lint-cross-product-env.mjs'

describe('no-cross-product-env-vars', () => {
  it('flags a REPORTS_ env var in TS source', () => {
    expect(scanText('const k = process.env.REPORTS_API_KEY')).toEqual(['REPORTS_API_KEY'])
  })

  it('flags a REPORTS_ env var accessed from Python', () => {
    expect(scanText('os.environ["REPORTS_DB_URL"]')).toContain('REPORTS_DB_URL')
  })

  it('flags multiple distinct cross-product vars', () => {
    const found = scanText('REPORTS_A and REPORTS_B and REPORTS_A')
    expect(found.sort()).toEqual(['REPORTS_A', 'REPORTS_B'])
  })

  it('ignores Viabe Team env vars', () => {
    expect(scanText('process.env.FOUNDING_PRICE_PAISE')).toEqual([])
    expect(scanText('process.env.STANDARD_PRICE_PAISE')).toEqual([])
  })

  it('does not match a bare prefix with no suffix', () => {
    expect(scanText('the REPORTS_ prefix is reserved')).toEqual([])
  })

  it('flags deprecated Supabase keys', () => {
    expect(scanText('process.env.SUPABASE_SERVICE_ROLE_KEY')).toEqual([
      'SUPABASE_SERVICE_ROLE_KEY',
    ])
    expect(scanText('const k = SUPABASE_ANON_KEY')).toContain('SUPABASE_ANON_KEY')
  })

  it('allows the permitted TEAM_SUPABASE_* keys', () => {
    expect(
      scanText('TEAM_SUPABASE_PUBLISHABLE_KEY and TEAM_SUPABASE_SECRET_KEY'),
    ).toEqual([])
  })

  it('keeps the repo free of forbidden env vars', () => {
    expect(scanRepo().violations).toEqual([])
  })
})
