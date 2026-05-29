/**
 * VT-233 — safeNext shared allowlist tests.
 */

import { describe, expect, it } from 'vitest'

import { safeNext } from '@/lib/auth/safe-next'

describe('VT-233 — safeNext', () => {
  it('returns default for null/undefined/empty', () => {
    expect(safeNext(null)).toBe('/team/ops')
    expect(safeNext(undefined)).toBe('/team/ops')
    expect(safeNext('')).toBe('/team/ops')
  })

  it('preserves allowlisted exact match', () => {
    expect(safeNext('/team/ops')).toBe('/team/ops')
    expect(safeNext('/team/onboard')).toBe('/team/onboard')
    expect(safeNext('/team/dashboard')).toBe('/team/dashboard')
  })

  it('preserves allowlisted subpath', () => {
    expect(safeNext('/team/ops/stream')).toBe('/team/ops/stream')
    expect(safeNext('/team/dashboard/feedback')).toBe('/team/dashboard/feedback')
  })

  it('rejects external URL → default', () => {
    expect(safeNext('//evil.com/xss')).toBe('/team/ops')
    expect(safeNext('https://evil.com')).toBe('/team/ops')
  })

  it('rejects path traversal → default', () => {
    expect(safeNext('/team/../etc/passwd')).toBe('/team/ops')
  })

  it('rejects non-/team paths → default', () => {
    expect(safeNext('/login')).toBe('/team/ops')
    expect(safeNext('/api/admin')).toBe('/team/ops')
  })

  it('rejects /team/<unknown>', () => {
    expect(safeNext('/team/secret')).toBe('/team/ops')
  })
})
