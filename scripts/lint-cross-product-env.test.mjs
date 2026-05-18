import { describe, expect, it } from 'vitest'
import { envVarViolation, scanRepo, scanText } from './lint-cross-product-env.mjs'

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

  it('flags server secrets in the web app env file', () => {
    expect(envVarViolation('frontend', 'TEAM_SUPABASE_SECRET_KEY')).toMatch(/web app env/)
    expect(envVarViolation('frontend', 'TEAM_ANTHROPIC_API_KEY')).toMatch(/web app env/)
    // The unprefixed name is NOT whitelisted — a typo trap (use TEAM_TWILIO_AUTH_TOKEN).
    expect(envVarViolation('frontend', 'TWILIO_AUTH_TOKEN')).toMatch(/web app env/)
  })

  it('allows NEXT_PUBLIC_ and non-secret vars in the web app env file', () => {
    expect(envVarViolation('frontend', 'NEXT_PUBLIC_TEAM_SUPABASE_URL')).toBeNull()
    expect(envVarViolation('frontend', 'NEXT_PUBLIC_TEAM_SUPABASE_PUBLISHABLE_KEY')).toBeNull()
    expect(envVarViolation('frontend', 'NEXT_PUBLIC_SITE_URL')).toBeNull()
  })

  it('flags NEXT_PUBLIC_ vars in a backend app env file', () => {
    expect(envVarViolation('backend', 'NEXT_PUBLIC_TEAM_SUPABASE_URL')).toMatch(/backend app env/)
  })

  it('allows server secrets in a backend app env file', () => {
    expect(envVarViolation('backend', 'TEAM_SUPABASE_SECRET_KEY')).toBeNull()
    expect(envVarViolation('backend', 'TEAM_TWILIO_AUTH_TOKEN')).toBeNull()
    expect(envVarViolation('backend', 'INTERNAL_API_SECRET')).toBeNull()
  })

  it('whitelists team-web route-handler server vars (VT-3.3b)', () => {
    expect(envVarViolation('frontend', 'TEAM_TWILIO_AUTH_TOKEN')).toBeNull()
    expect(envVarViolation('frontend', 'INTERNAL_API_SECRET')).toBeNull()
    // A non-whitelisted server secret in the web env is still rejected.
    expect(envVarViolation('frontend', 'SOME_OTHER_API_KEY')).toMatch(/web app env/)
  })

  it('keeps the repo free of forbidden env vars', () => {
    expect(scanRepo().violations).toEqual([])
  })
})
