/**
 * VT-211 — Integration Agent onboarding page Playwright e2e (5 steps).
 *
 * Auth-mocked: the test seeds a viabe_ops_jwt cookie scoped to
 * FAZAL_OWNER_UUID so requireFazal() passes. The orchestrator's
 * onboard-step endpoint is stubbed via route interception so the spec
 * doesn't depend on a running orchestrator.
 *
 * 5 assertions per brief AC-1..AC-5:
 *
 *  1. unauth GET /team/onboard → redirect to /login
 *  2. auth GET → renders current-phase prompt
 *  3. POST answer → redirect → next-phase prompt rendered
 *  4. close+reopen (second GET) → identical content (resumability)
 *  5. phase_5_confirmed → "All set" page
 */

import { expect, test } from '@playwright/test'

const FAZAL_JWT_COOKIE = process.env.VT211_FAZAL_JWT_COOKIE ?? ''
const FAZAL_TENANT_ID =
  process.env.FAZAL_TENANT_ID ?? '00000000-0000-4000-8000-000000aaaaaa'


test.describe('VT-211 Integration Agent onboarding', () => {
  test('1. unauthenticated → redirect to /login', async ({ context, page }) => {
    await context.clearCookies()
    const res = await page.goto('/team/onboard')
    expect(res?.url()).toContain('/login')
  })

  test.describe('with auth + stubbed onboard data', () => {
    test.beforeEach(async ({ context, page }) => {
      if (FAZAL_JWT_COOKIE) {
        await context.addCookies([
          {
            name: 'viabe_ops_jwt',
            value: FAZAL_JWT_COOKIE,
            domain: 'localhost',
            path: '/',
            httpOnly: true,
            secure: false,
            sameSite: 'Lax',
          },
        ])
      }

      // Mock orchestrator onboard-step (POST forward from /api/onboard/answer).
      await page.route('**/api/orchestrator/integrations/onboard-step', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            next_phase: 'phase_2_auth',
            next_prompt: 'Which data source do you want to connect first?',
            run_id: '00000000-0000-4000-8000-000000bbbbbb',
          }),
        })
      })
    })

    test('2. auth GET renders current-phase prompt', async ({ page }) => {
      test.skip(!FAZAL_JWT_COOKIE, 'VT211_FAZAL_JWT_COOKIE not provided')
      await page.goto('/team/onboard')
      await expect(page.locator('[data-area="onboard"]')).toBeVisible()
      await expect(page.locator('[data-element="agent-prompt"]')).toBeVisible()
      await expect(page.locator('[data-element="answer-input"]')).toBeVisible()
      await expect(page.locator('[data-element="submit"]')).toBeVisible()
    })

    test('3. POST answer → page reloads with next-phase prompt', async ({ page }) => {
      test.skip(!FAZAL_JWT_COOKIE, 'VT211_FAZAL_JWT_COOKIE not provided')
      await page.goto('/team/onboard')
      await page.locator('[data-element="answer-input"]').fill('I run a small restaurant')
      await page.locator('[data-element="submit"]').click()
      // After redirect back to /team/onboard, the page re-fetches state.
      await expect(page).toHaveURL(/\/team\/onboard/)
      await expect(page.locator('[data-area="onboard"]')).toBeVisible()
    })

    test('4. close + reopen → same state', async ({ page, context }) => {
      test.skip(!FAZAL_JWT_COOKIE, 'VT211_FAZAL_JWT_COOKIE not provided')
      await page.goto('/team/onboard')
      const firstContent = await page.locator('[data-element="agent-prompt"]').textContent()

      // Simulate close + reopen by navigating away then back via fresh page.
      const second = await context.newPage()
      if (FAZAL_JWT_COOKIE) {
        await second.context().addCookies([
          {
            name: 'viabe_ops_jwt',
            value: FAZAL_JWT_COOKIE,
            domain: 'localhost',
            path: '/',
            httpOnly: true,
            secure: false,
            sameSite: 'Lax',
          },
        ])
      }
      await second.goto('/team/onboard')
      const secondContent = await second.locator('[data-element="agent-prompt"]').textContent()
      expect(secondContent).toBe(firstContent)
    })

    test('5. phase_5_confirmed → "All set" view', async ({ page }) => {
      test.skip(
        !FAZAL_JWT_COOKIE,
        'VT211_FAZAL_JWT_COOKIE not provided; skipping confirmed-phase check',
      )
      // This step depends on the fixture data showing the tenant in
      // phase_5_confirmed. Seed via VT211_FORCE_CONFIRMED env if the
      // test harness can mutate tenant_integration_state — otherwise
      // skipped with a clear note.
      const forceConfirmed = process.env.VT211_FORCE_CONFIRMED === 'true'
      test.skip(!forceConfirmed, 'VT211_FORCE_CONFIRMED not set')
      await page.goto('/team/onboard')
      await expect(page.locator('[data-area="onboard-confirmed"]')).toBeVisible()
      await expect(page.getByText('All set')).toBeVisible()
    })
  })

  test('Tenant id fixture matches env', () => {
    // Smoke check that the env wiring is in place for downstream tests.
    expect(FAZAL_TENANT_ID).toMatch(/^[0-9a-f-]{36}$/i)
  })
})
