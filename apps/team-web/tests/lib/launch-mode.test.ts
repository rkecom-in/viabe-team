/** VT-97 — the build-time launch-mode toggle + the waitlist field validator. */

import { afterEach, describe, expect, it } from 'vitest'

import { launchMode } from '@/lib/launch-mode'
import { waitlistFieldsValid } from '@/app/(marketing)/team/waitlist-form'

describe('VT-97 launchMode', () => {
  const orig = process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE
  afterEach(() => {
    if (orig === undefined) delete process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE
    else process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE = orig
  })

  it('defaults to waitlist (the honest pre-launch state)', () => {
    delete process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE
    expect(launchMode()).toBe('waitlist')
  })

  it('honors live + maintenance', () => {
    process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE = 'live'
    expect(launchMode()).toBe('live')
    process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE = 'maintenance'
    expect(launchMode()).toBe('maintenance')
  })

  it('an unknown value falls back to waitlist (never an undefined mode)', () => {
    process.env.NEXT_PUBLIC_TEAM_LAUNCH_MODE = 'nonsense'
    expect(launchMode()).toBe('waitlist')
  })
})

describe('VT-97 waitlistFieldsValid (consent gates)', () => {
  it('requires a valid email, a +91 mobile, AND consent', () => {
    expect(waitlistFieldsValid('a@b.com', '+919876543210', true)).toBe(true)
    expect(waitlistFieldsValid('a@b.com', '+919876543210', false)).toBe(false) // no consent
    expect(waitlistFieldsValid('notanemail', '+919876543210', true)).toBe(false)
    expect(waitlistFieldsValid('a@b.com', '+12025551234', true)).toBe(false) // non-+91
    expect(waitlistFieldsValid('', '', true)).toBe(false)
  })
})
