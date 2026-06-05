import { describe, expect, it } from 'vitest'

import { foundingDisplayState } from '@/app/(marketing)/team/founding-counter-widget'

describe('foundingDisplayState (VT-99 — honest framing)', () => {
  it('null status -> unknown (loading / error fallback)', () => {
    expect(foundingDisplayState(null).kind).toBe('unknown')
  })

  it('below 90 claimed -> available, NO urgency framing', () => {
    const d = foundingDisplayState({ remaining: 50, cap: 100, public_count: 50, all_claimed: false })
    expect(d).toMatchObject({ kind: 'available', almostFull: false, remaining: 50, claimed: 50 })
  })

  it('>=90 claimed -> available + almostFull (urgency only here)', () => {
    const d = foundingDisplayState({ remaining: 5, cap: 100, public_count: 95, all_claimed: false })
    expect(d).toMatchObject({ kind: 'available', almostFull: true, remaining: 5 })
  })

  it('all_claimed -> full', () => {
    expect(
      foundingDisplayState({ remaining: 0, cap: 100, public_count: 100, all_claimed: true }).kind,
    ).toBe('full')
  })

  it('remaining<=0 -> full even if the all_claimed flag is stale', () => {
    expect(
      foundingDisplayState({ remaining: 0, cap: 100, public_count: 100, all_claimed: false }).kind,
    ).toBe('full')
  })
})
