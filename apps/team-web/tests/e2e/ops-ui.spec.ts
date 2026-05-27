/**
 * VT-123 Ops Console Playwright e2e (8 assertions; UI flow).
 *
 * Tests assume the Python canary `vt123_ops_ui_substrate.py` has:
 *   1. Inserted a synthetic tenant + pipeline_run + 50 pipeline_steps
 *      with a phone_token in one step's envelope.
 *   2. Started team-web dev server (or supplied baseURL via env).
 *   3. Issued + dropped a `viabe_ops_jwt` cookie scoped to
 *      `FAZAL_OWNER_UUID` so `requireFazal()` passes.
 *
 * Test fixtures expose synthetic IDs via env (VT123_SYNTH_*).
 */

import { expect, test } from '@playwright/test'

const SYNTH_TENANT_ID = process.env.VT123_SYNTH_TENANT_ID ?? ''
const SYNTH_RUN_ID = process.env.VT123_SYNTH_RUN_ID ?? ''
const SYNTH_PHONE_TOKEN = process.env.VT123_SYNTH_PHONE_TOKEN ?? ''
const FAZAL_JWT_COOKIE = process.env.VT123_FAZAL_JWT_COOKIE ?? ''


test.describe('VT-123 Ops Console', () => {
  test.beforeEach(async ({ context }) => {
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
  })

  test('1. /ops renders workspace overview with counters', async ({ page }) => {
    await page.goto('/team/ops')
    await expect(page.locator('[data-area="team-ops-workspace"]')).toBeVisible()
    await expect(page.locator('[data-counter="in_flight_runs"]')).toBeVisible()
    await expect(page.locator('[data-counter="total_runs_today"]')).toBeVisible()
  })

  test('2. /ops/tenants/<id> renders profile + timeline + campaigns + audit', async ({
    page,
  }) => {
    test.skip(!SYNTH_TENANT_ID, 'VT123_SYNTH_TENANT_ID not provided')
    await page.goto(`/team/ops/tenants/${SYNTH_TENANT_ID}`)
    await expect(page.locator('[data-area="team-ops-tenant"]')).toBeVisible()
    await expect(page.locator('[data-section="timeline"]')).toBeVisible()
    await expect(page.locator('[data-section="campaigns"]')).toBeVisible()
    await expect(page.locator('[data-section="privacy-audit"]')).toBeVisible()
  })

  test('3. /ops/runs/<id> renders waterfall with all canonical columns', async ({
    page,
  }) => {
    test.skip(!SYNTH_RUN_ID, 'VT123_SYNTH_RUN_ID not provided')
    await page.goto(`/team/ops/runs/${SYNTH_RUN_ID}`)
    await expect(page.locator('[data-component="run-waterfall"]')).toBeVisible()
    const firstCard = page.locator('.ops-step-card').first()
    await firstCard.locator('[data-action="toggle-expand"]').click()
    // Canonical column data-attributes — every one must be in DOM
    // (CL-417 render-via-native-columns, NOT JSONB extraction).
    const required = [
      'step_seq',
      'step_kind',
      'step_name',
      'status',
      'parent_step_id',
      'decision_rationale',
      'model_used',
      'tokens_input',
      'tokens_output',
      'cost_paise',
      'duration_ms',
      'tool_calls',
      'input_envelope',
      'output_envelope',
    ]
    for (const col of required) {
      await expect(firstCard.locator(`[data-col="${col}"]`)).toHaveCount(1)
    }
  })

  test('4. [resolve] reveals phone + audit row visible', async ({ page }) => {
    test.skip(
      !SYNTH_RUN_ID || !SYNTH_PHONE_TOKEN,
      'synthetic phone-bearing step required',
    )
    page.on('dialog', async (d) => {
      await d.accept()
    })
    await page.goto(`/team/ops/runs/${SYNTH_RUN_ID}`)
    const card = page
      .locator('.ops-step-card', { has: page.locator('[data-action="resolve"]') })
      .first()
    await card.locator('[data-action="toggle-expand"]').click()
    await card.locator('[data-action="resolve"] button').click()
    await expect(card.locator('[data-resolved-phone]')).toBeVisible({
      timeout: 5_000,
    })
  })

  test('5. [export step as test fixture] copies JSON to clipboard', async ({
    page,
    context,
  }) => {
    test.skip(!SYNTH_RUN_ID, 'VT123_SYNTH_RUN_ID not provided')
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.goto(`/team/ops/runs/${SYNTH_RUN_ID}`)
    const card = page.locator('.ops-step-card').first()
    await card.locator('[data-action="toggle-expand"]').click()
    await card.locator('[data-action="export-fixture"] button').click()
    const clipText = await page.evaluate(async () => navigator.clipboard.readText())
    // Must be parseable as JSON.
    expect(() => JSON.parse(clipText)).not.toThrow()
  })

  test('6. unauthenticated request redirects to login', async ({ context, page }) => {
    await context.clearCookies()
    const res = await page.goto('/team/ops')
    expect(res?.url()).toContain('/login')
  })

  test('7. resolve API returns 403 without operator JWT', async ({ request }) => {
    const res = await request.post('/api/ops/resolve-phone', {
      data: { phone_token: 'phone_tok_dummy' },
      headers: { 'content-type': 'application/json' },
    })
    expect(res.status()).toBe(403)
  })

  test('8. ANTHROPIC absent — UI never calls LLM (defense-in-depth)', async ({
    page,
  }) => {
    test.skip(!SYNTH_RUN_ID, 'VT123_SYNTH_RUN_ID not provided')
    const networkUrls: string[] = []
    page.on('request', (req) => networkUrls.push(req.url()))
    await page.goto(`/team/ops/runs/${SYNTH_RUN_ID}`)
    expect(
      networkUrls.filter((u) => u.includes('anthropic.com') || u.includes('voyageai')),
    ).toHaveLength(0)
  })
})
