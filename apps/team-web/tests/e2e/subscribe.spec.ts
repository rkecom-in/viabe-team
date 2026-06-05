/**
 * VT-91 — /team/subscribe (card-capture) Playwright e2e.
 *
 * Auth-mocked, mirroring onboard.spec: the auth'd render tests are gated on
 * CI-provided minted credentials (a portal owner-session cookie + a trial-end token)
 * and SKIP when those env vars are absent — until Cowork provisions VT91_OWNER_COOKIE /
 * VT91_TRIAL_TOKEN (minted with OWNER_JWT_SECRET). The unauth-redirect assertion runs
 * anywhere. The 3-path auth resolver + IDOR are fully covered by the vitest unit suite
 * (tests/api/razorpay-subscribe.test.ts + tests/lib/verify-trial-end-token.test.ts).
 */

import { expect, test } from '@playwright/test'

const OWNER_COOKIE = process.env.VT91_OWNER_COOKIE ?? ''
const TRIAL_TOKEN = process.env.VT91_TRIAL_TOKEN ?? ''

test.describe('VT-91 subscribe / card capture', () => {
  test('unauthenticated → redirect to /team/login', async ({ context, page }) => {
    await context.clearCookies()
    const res = await page.goto('/team/subscribe?plan=standard')
    expect(res?.url()).toContain('/team/login')
  })

  test('portal session → renders the Subscribe button', async ({ context, page }) => {
    test.skip(!OWNER_COOKIE, 'VT91_OWNER_COOKIE not provisioned')
    await context.addCookies([
      {
        name: 'viabe_team_session',
        value: OWNER_COOKIE,
        domain: 'localhost',
        path: '/',
        httpOnly: true,
        secure: false,
        sameSite: 'Lax',
      },
    ])
    await page.goto('/team/subscribe?plan=standard')
    await expect(page.locator('[data-action="subscribe"]')).toBeVisible()
  })

  test('trial-end deep-link token → renders the Subscribe button (no portal login)', async ({
    context,
    page,
  }) => {
    test.skip(!TRIAL_TOKEN, 'VT91_TRIAL_TOKEN not provisioned')
    await context.clearCookies()
    await page.goto(`/team/subscribe?plan=standard&token=${TRIAL_TOKEN}`)
    await expect(page.locator('[data-action="subscribe"]')).toBeVisible()
  })
})
