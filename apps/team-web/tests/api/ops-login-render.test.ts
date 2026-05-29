/**
 * VT-232 — Ops Console login render smoke test.
 *
 * A1: page source contains no `data-area="team-ops"` banner sentinel
 *     (banner is in (app)/team/ops/layout.tsx; (auth) route group skips it)
 * A2: page source contains `name="email"` input + `type="submit"` button
 *     (form is renderable)
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

describe('VT-232 — ops login render', () => {
  let src = ''

  beforeEach(async () => {
    const { readFile } = await import('fs/promises')
    const path = await import('path')
    const filePath = path.resolve(
      process.cwd(),
      'app/(auth)/team/ops/login/page.tsx',
    )
    src = await readFile(filePath, 'utf8')
  })

  afterEach(() => {
    src = ''
  })

  it('A1 — login page source does NOT match (app)-shell banner sentinel', () => {
    // The (app)/team/ops/layout.tsx wraps its children with
    // <StickyBannerLive data-component="sticky-banner-live"/>; this
    // page must NOT carry that data-area marker.
    expect(src).not.toContain('data-area="team-ops"')
    // The login page MUST self-identify so other tests can grep it
    expect(src).toContain('data-area="team-ops-login"')
  })

  it('A2 — login page source contains email input + submit button', () => {
    expect(src).toMatch(/name="email"/)
    expect(src).toMatch(/type="submit"/)
    expect(src).toContain('/api/ops/login')
  })

  it('A3 — login page source has Tailwind utility classes (presentation usable)', () => {
    // Smoke-check that the page isn't an unstyled HTML stack.
    expect(src).toMatch(/min-h-screen|flex|rounded/)
  })
})
