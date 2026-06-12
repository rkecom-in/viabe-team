/**
 * VT-375 (Phase B) — Run-Control canvas e2e (READ-ONLY surface).
 *
 * Target: /team/ops/run-control — the de-identified VTR run-control canvas
 * (tenant tiles → program tiles → step timeline). Built this session:
 *   - app/(app)/team/ops/run-control/page.tsx        (server component)
 *   - app/(app)/team/ops/run-control/run-control-canvas.tsx (client tiles)
 *   - lib/orchestrator-client.ts :: vtrPrograms / vtrRunTimeline (GET reads)
 *
 * Auth: minted operator JWT via _jwt-fixture (cookie scoped to FAZAL_OWNER_UUID
 * → VTAdmin). Requires OPERATOR_JWT_SECRET + FAZAL_OWNER_UUID in env (CI: the
 * e2e-playwright job provisions them; local: source .viabe/secrets/supabase-dev.env).
 *
 * ── DATA-PATH NOTE (why page.route() can't drive the canvas) ─────────────────
 * The canvas data is fetched ENTIRELY server-side by the React server component:
 *   1. fetchTopTenants()  → Supabase RPC via serverSecretClient()
 *   2. vtrPrograms()/vtrRunTimeline() → server-side fetch() to TEAM_ORCHESTRATOR_URL
 * Neither is a browser-originated request, so Playwright's page.route() interceptor
 * (which only sees browser traffic) CANNOT stub them — exactly like onboard.spec's
 * server-forward path. The reachable render in a stubless CI/local env is therefore
 * the DEGRADED one:
 *   - serverSecretClient() THROWS when SUPABASE_URL / SUPABASE_SECRET_KEY are unset
 *     (CI sets only NEXT_PUBLIC_SUPABASE_URL) → page catches → `loadError` set
 *     → renders the red [data-section-error] "couldn't load tenants" banner.
 *   - With real Supabase creds but no orchestrator, fetchTopTenants returns rows
 *     and vtrPrograms fails-closed degraded=true → the in-canvas rc-degraded-banner.
 *
 * Coverage strategy (per VT-372 rendered standard + the prompt's allowance for
 * bundle-level assertion of paths a stubless harness can't exercise):
 *   T1  authed render + ZERO browser console errors + screenshots (degraded render).
 *   T2  the reachable degraded banner ([data-section-error] "couldn't load").
 *   T3  the binding honesty copy + data-testid tokens are PRESENT in the served
 *       client JS bundle (the canvas component is 'use client' so its verbatim
 *       strings ship to the browser even when the canvas itself doesn't mount).
 *       This is the bundle-level guarantee for strings whose render path needs a
 *       live Supabase+orchestrator backing stack (see RC_E2E_STUB_BACKED below).
 *   T4  FULL canvas render — testids (rc-tenant-tile/rc-program-tile/rc-timeline-row/
 *       rc-degraded-banner) + rendered honesty copy — driven by a real Supabase
 *       (tenants) + a local http stub orchestrator (programs/timeline). SKIP-gated
 *       on RC_E2E_STUB_BACKED=1 because it needs SUPABASE_URL/SUPABASE_SECRET_KEY
 *       pointed at a DB with tenant rows AND the team-web server started with
 *       TEAM_ORCHESTRATOR_URL pointing at this spec's stub. Self-skips otherwise.
 *
 * Paths this spec does NOT render-exercise in the default (stubless) run, and why:
 *   - rc-tenant-tile / rc-program-tile / rc-timeline-row / rc-degraded-banner DOM
 *     (need tenants from Supabase + a programs/timeline payload from the orchestrator)
 *   - "Observed — not controllable" badge, "Keys only" disclosure, holds footer
 *     "no guaranteed order" RENDERED (need a timeline/holds payload)
 *   These are asserted at the bundle level in T3 and rendered in T4 (when stub-backed).
 */

import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'

import { expect, test } from './_jwt-fixture'

const RUN_CONTROL_PATH = '/team/ops/run-control'

// ── Binding honesty copy (must match run-control-canvas.tsx verbatim) ────────
const COPY = {
  observedBadge: 'Observed — not controllable',
  keysOnly: 'Keys only — values are de-identified by construction.',
  degraded: 'Pause state unverifiable right now — control reads are degraded.',
  holdsFooter: 'Concurrently-held runs release in no guaranteed order.',
  rerunLineage:
    'Re-dispatched as a NEW run (no time-travel) — prior steps re-execute only if the entry point requires them.',
} as const

// ── VT-376 interactive-control copy (must match run-control-controls.tsx verbatim) ──
const RC_COPY = {
  blindWrite: 'Keys only — you are editing values you cannot see.',
  consumeFail:
    'If control reads degrade, a pin may silently not apply — check the timeline after the run.',
  rerunI2: 'Re-running re-enters owner approval — outputs are not auto-kept.',
  preflightWarn: 'Owner approval pending — rerun will refuse. Resolve the approval first.',
  escalatedOverlap:
    'An owner approval armed during this re-run — the run was ESCALATED, not silently kept. ' +
    'Check the escalation queue.',
} as const

// data-testid tokens the canvas emits (each appears verbatim in the client bundle).
const TESTID_TOKENS = [
  'rc-tenant-tile',
  'rc-program-tile',
  'rc-timeline-row',
  'rc-degraded-banner',
] as const

// Browser console-error allowlist: favicon/manifest 404s + Next resource-load
// noise are environment artifacts, not page bugs. (No existing ops spec keeps an
// allowlist — there is no inherited list to copy — so this is the minimal honest one.)
const CONSOLE_ERROR_ALLOWLIST = [
  /favicon/i,
  /manifest/i,
  /\/business-types/i, // mirrors the prompt's known-artifact carve-out
  /Failed to load resource.*404/i,
]

function isAllowedConsoleError(text: string): boolean {
  return CONSOLE_ERROR_ALLOWLIST.some((re) => re.test(text))
}

// [B3 VT-380] PAGE_ERROR_ALLOWLIST removed — sticky-banner.tsx now renders a deterministic
// UTC HH:MM:SS timestamp (utcTimeString()) instead of toLocaleTimeString(), eliminating
// React minified error #418 at root. No hydration allowlist needed.

/**
 * Collect ALL the JS the page pulls in: every <script src> chunk plus any inline
 * script. The canvas component is 'use client', so its module — including the
 * verbatim honesty-copy string literals and the data-testid tokens — is shipped
 * as one of these chunks regardless of whether the canvas mounts this render.
 */
async function collectServedJs(
  request: import('@playwright/test').APIRequestContext,
  baseURL: string,
  html: string,
): Promise<string> {
  const srcs = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)]
    .map((m) => m[1])
    .filter((s): s is string => Boolean(s))
  const inline = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1] ?? '')
    .join('\n')
  const chunks = await Promise.all(
    srcs.map(async (src) => {
      const url = src.startsWith('http') ? src : `${baseURL}${src.startsWith('/') ? '' : '/'}${src}`
      try {
        const r = await request.get(url)
        return r.ok() ? await r.text() : ''
      } catch {
        return ''
      }
    }),
  )
  return [html, inline, ...chunks].join('\n')
}

test.describe('VT-375 Run-Control canvas (read-only)', () => {
  test('1. renders authed with zero browser console errors + screenshots', async ({
    page,
    fazalJwt,
  }) => {
    expect(fazalJwt).toBeTruthy()

    const consoleErrors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text())
    })
    // pageerror = uncaught exceptions in page scripts; those are never acceptable.
    const pageErrors: string[] = []
    page.on('pageerror', (err) => pageErrors.push(err.message))

    const res = await page.goto(RUN_CONTROL_PATH)
    // Auth passed (fixture cookie) — must NOT bounce to the ops login.
    expect(
      page.url(),
      `landed on ${page.url()} (status ${res?.status() ?? '?'})`,
    ).not.toContain('/login')
    // Route exists + renders its shell (server component degrades section-by-section,
    // so even with no backing stack the <main> shell is present, HTTP 200).
    expect(res?.status()).toBe(200)
    await expect(page.locator('[data-area="team-ops-run-control"]')).toBeVisible({
      timeout: 10_000,
    })
    await expect(
      page.getByRole('heading', { name: /Run Control/i }),
    ).toBeVisible()

    // fullPage screenshots — desktop 1280 then 390px mobile.
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.waitForTimeout(200)
    await page.screenshot({ path: '/tmp/vt375-rc-desktop.png', fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.waitForTimeout(200)
    await page.screenshot({ path: '/tmp/vt375-rc-mobile.png', fullPage: true })

    // Zero uncaught page exceptions — no allowlist; banner #418 is fixed at root (VT-380/B3).
    expect(
      pageErrors,
      `unexpected pageerrors: ${pageErrors.join(' | ')}`,
    ).toHaveLength(0)
    const disallowed = consoleErrors.filter((t) => !isAllowedConsoleError(t))
    expect(
      disallowed,
      `unexpected console errors: ${disallowed.join(' | ')}`,
    ).toHaveLength(0)
  })

  test('2. degrades to the "couldn\'t load" banner with no backing stack', async ({
    page,
    fazalJwt,
  }) => {
    expect(fazalJwt).toBeTruthy()
    const res = await page.goto(RUN_CONTROL_PATH)
    expect(res?.status()).toBe(200)
    expect(page.url()).not.toContain('/login')

    // Without SUPABASE_URL/SECRET_KEY, fetchTopTenants throws → page sets loadError
    // → the red [data-section-error] banner renders. (If real Supabase creds ARE
    // present, the tenant load may succeed and the page shows the canvas or the
    // "No tenants in scope." empty state instead — accept any of the three honest
    // degraded/empty renders, all of which prove the route degrades rather than 500s.)
    const sectionError = page.locator('[data-section-error]').first()
    const emptyState = page.getByText('No tenants in scope.')
    const canvas = page.locator('[data-rc-canvas]')

    await expect
      .poll(
        async () =>
          (await sectionError.count()) +
          (await emptyState.count()) +
          (await canvas.count()),
        { timeout: 10_000 },
      )
      .toBeGreaterThan(0)

    // When it's the tenant-load failure path, the copy must read "couldn't load".
    if ((await sectionError.count()) > 0) {
      await expect(sectionError).toContainText(/couldn.{1,3}t load tenants/i)
    }
  })

  test('3. binding honesty copy + data-testid tokens are present in the served JS bundle', async ({
    page,
    request,
    baseURL,
    fazalJwt,
  }) => {
    expect(fazalJwt).toBeTruthy()
    test.skip(!baseURL, 'baseURL required to fetch JS chunks')
    await page.goto(RUN_CONTROL_PATH)
    const html = await page.content()
    const bundle = await collectServedJs(request, baseURL!, html)

    // Each honesty string is a verbatim literal in run-control-canvas.tsx ('use client'),
    // so it must appear in the page HTML or one of its client JS chunks even when the
    // canvas DOM isn't mounted (degraded render). This is the bundle-level guarantee for
    // the copy whose RENDERED path needs a live backing stack (exercised in T4).
    const missingCopy = Object.entries(COPY).filter(([, s]) => !bundle.includes(s))
    expect(
      missingCopy.map(([k]) => k),
      `honesty copy missing from served JS: ${missingCopy.map(([k]) => k).join(', ')}`,
    ).toHaveLength(0)

    const missingTokens = TESTID_TOKENS.filter((t) => !bundle.includes(t))
    expect(
      missingTokens,
      `data-testid tokens missing from served JS: ${missingTokens.join(', ')}`,
    ).toHaveLength(0)
  })

  // ── T4: full canvas render via a local stub orchestrator + real Supabase ──────
  // SKIP-gated: needs the team-web server started with SUPABASE_URL/SUPABASE_SECRET_KEY
  // pointed at a DB that returns ≥1 tenant from ops_top_tenants_today, AND with
  // TEAM_ORCHESTRATOR_URL pointed at the stub this test starts. Set RC_E2E_STUB_BACKED=1
  // (and start the stub on RC_STUB_PORT, default 8001) to opt in. Self-skips otherwise.
  test.describe('4. full canvas render (stub-backed)', () => {
    let stub: Server | null = null
    let stubPort = 0

    test.beforeAll(async () => {
      if (process.env.RC_E2E_STUB_BACKED !== '1') return
      const port = Number(process.env.RC_STUB_PORT ?? 8001)
      stub = createServer((req, res) => {
        const url = req.url ?? ''
        res.setHeader('content-type', 'application/json')
        // /api/orchestrator/ops/run-control/programs/<tenantId>
        if (url.includes('/run-control/programs/')) {
          res.statusCode = 200
          res.end(
            JSON.stringify({
              past: [
                {
                  run_id: 'run-past-0001',
                  run_type: 'daily_brief',
                  status: 'completed',
                  started_at: new Date(Date.now() - 86_400_000).toISOString(),
                  ended_at: new Date(Date.now() - 86_300_000).toISOString(),
                  rerun_of_run_id: null,
                  rerun_from_step: null,
                  step_count: 3,
                },
              ],
              running: [
                {
                  run_id: 'run-live-0001',
                  run_type: 'campaign',
                  status: 'running',
                  started_at: new Date(Date.now() - 60_000).toISOString(),
                  ended_at: null,
                  rerun_of_run_id: null,
                  rerun_from_step: null,
                  step_count: 2,
                  active_hold: true,
                },
              ],
              upcoming_7d: [
                {
                  kind: 'trial_sweep',
                  due_at: new Date(Date.now() + 86_400_000).toISOString(),
                  label: 'Trial ends',
                  source: 'config/trial.yaml',
                },
              ],
              // Non-empty holds → exercises the holds footer "no guaranteed order" copy.
              holds: [{ workflow_kind: 'campaign', set_at: new Date().toISOString() }],
              // degraded=true → exercises the in-canvas rc-degraded-banner.
              degraded: true,
            }),
          )
          return
        }
        // /api/orchestrator/ops/run-control/timeline/<runId>
        if (url.includes('/run-control/timeline/')) {
          res.statusCode = 200
          res.end(
            JSON.stringify({
              run_id: 'run-live-0001',
              tenant_id: 'tenant-stub-0001',
              steps: [
                {
                  run_id: 'run-live-0001',
                  step_id: 'step-1',
                  step_seq: 1,
                  step_kind: 'classify',
                  step_name: 'intent',
                  step_status: 'completed',
                  tier: 'observed', // → "Observed — not controllable" badge
                  duration_ms: 420,
                  override_id: null,
                  paused_ms: null,
                  input_envelope: ['message_id', 'lang'], // keys-only
                  output_envelope: { intent: null },
                },
              ],
              active_controls: [],
            }),
          )
          return
        }
        res.statusCode = 404
        res.end(JSON.stringify({ error: 'not_found' }))
      })
      await new Promise<void>((resolve) => stub!.listen(port, resolve))
      stubPort = (stub!.address() as AddressInfo).port
      console.log(`[run-control.spec] stub orchestrator on :${stubPort}`)
    })

    test.afterAll(async () => {
      if (stub) await new Promise<void>((resolve) => stub!.close(() => resolve()))
    })

    test('renders tenant/program/timeline tiles + honesty copy', async ({
      page,
      fazalJwt,
    }) => {
      test.skip(
        process.env.RC_E2E_STUB_BACKED !== '1',
        'RC_E2E_STUB_BACKED!=1 — needs real Supabase tenants + the team-web server ' +
          'started with TEAM_ORCHESTRATOR_URL → this stub. See file header.',
      )
      expect(fazalJwt).toBeTruthy()
      await page.goto(RUN_CONTROL_PATH)

      const tenantTile = page.locator('[data-testid="rc-tenant-tile"]').first()
      await expect(tenantTile).toBeVisible({ timeout: 10_000 })
      // Expand the tenant tile → program groups + degraded banner + holds footer.
      await tenantTile.locator('button').first().click()

      await expect(
        page.locator('[data-testid="rc-degraded-banner"]').first(),
      ).toContainText(COPY.degraded)
      await expect(
        page.locator('[data-rc-holds-footer]').first(),
      ).toContainText(COPY.holdsFooter)

      const programTile = page.locator('[data-testid="rc-program-tile"]').first()
      await expect(programTile).toBeVisible()

      // The running program tile auto-expands its timeline → timeline rows + the
      // observed-tier badge + the keys-only envelope disclosure must render.
      const timelineRow = page.locator('[data-testid="rc-timeline-row"]').first()
      await expect(timelineRow).toBeVisible({ timeout: 10_000 })
      await expect(
        page.locator('[data-rc-observed-badge]').first(),
      ).toContainText(COPY.observedBadge)
      await expect(page.getByText(COPY.keysOnly).first()).toBeVisible()
    })
  })

  // ── T5: VT-376 interactive controls (stub-backed) ────────────────────────────
  // BINDING gate acceptance (build-contract §B2.4): the pre-flight warn state and the
  // escalated_overlap outcome rendering — NOT just the happy path. Same SKIP gate + inline
  // orchestrator stub as T4; this stub additionally serves the VT-376 run-level annotations
  // (rerunnable / forbidden_reason / open_approval), a CONTROLLABLE step (so the override +
  // rerun controls render), and a POST /run-control/rerun response.
  //
  // Two runs in the stub disambiguate the two legs:
  //   run-overlap-0001 → open_approval:true  → pre-flight WARN + disabled submit (leg a)
  //   run-clear-0001   → open_approval:false → submit enabled; the rerun POST returns
  //                        outcome:'escalated_overlap' → the C1-A disclosure renders (leg b)
  test.describe('5. VT-376 interactive controls (stub-backed)', () => {
    let stub: Server | null = null

    function programsBody() {
      return JSON.stringify({
        past: [],
        running: [
          {
            run_id: 'run-overlap-0001',
            run_type: 'agent_dispatch',
            status: 'running',
            started_at: new Date(Date.now() - 60_000).toISOString(),
            ended_at: null,
            rerun_of_run_id: null,
            rerun_from_step: null,
            step_count: 1,
            active_hold: false,
          },
          {
            run_id: 'run-clear-0001',
            run_type: 'agent_dispatch',
            status: 'running',
            started_at: new Date(Date.now() - 50_000).toISOString(),
            ended_at: null,
            rerun_of_run_id: null,
            rerun_from_step: null,
            step_count: 1,
            active_hold: false,
          },
        ],
        upcoming_7d: [],
        holds: [],
        degraded: false,
      })
    }

    // A controllable agent_dispatch step (candidate_build) so the override control renders, plus
    // the run-level VT-376 annotations. open_approval varies by run id (drives the pre-flight).
    function timelineBody(runId: string) {
      const openApproval = runId === 'run-overlap-0001'
      return JSON.stringify({
        run_id: runId,
        tenant_id: 'tenant-stub-0001',
        rerunnable: true,
        forbidden_reason: null,
        open_approval: openApproval,
        steps: [
          {
            run_id: runId,
            run_type: 'agent_dispatch',
            step_id: `${runId}-step-1`,
            step_seq: 1,
            step_kind: 'agent_dispatch',
            step_name: 'candidate_build',
            step_status: 'completed',
            tier: 'controllable',
            allowed_keys: ['limit'],
            duration_ms: 300,
            override_id: null,
            paused_ms: null,
            input_envelope: ['lead_id'],
            output_envelope: { count: null },
          },
        ],
        active_controls: [],
      })
    }

    test.beforeAll(async () => {
      if (process.env.RC_E2E_STUB_BACKED !== '1') return
      const port = Number(process.env.RC_STUB_PORT ?? 8001)
      stub = createServer((req, res) => {
        const url = req.url ?? ''
        res.setHeader('content-type', 'application/json')
        if (url.includes('/run-control/programs/')) {
          res.statusCode = 200
          res.end(programsBody())
          return
        }
        if (url.includes('/run-control/timeline/')) {
          const tail = url.split('/run-control/timeline/')[1] ?? ''
          const runId = decodeURIComponent(tail.split('?')[0] ?? '')
          res.statusCode = 200
          res.end(timelineBody(runId))
          return
        }
        // POST /run-control/rerun → the C1-A escalated-overlap close (still HTTP 200).
        if (url.includes('/run-control/rerun')) {
          res.statusCode = 200
          res.end(
            JSON.stringify({
              ok: true,
              new_run_id: 'run-new-0009',
              outcome: 'escalated_overlap',
              source_run_id: 'run-clear-0001',
              tenant_id: 'tenant-stub-0001',
            }),
          )
          return
        }
        res.statusCode = 404
        res.end(JSON.stringify({ error: 'not_found' }))
      })
      await new Promise<void>((resolve) => stub!.listen(port, resolve))
    })

    test.afterAll(async () => {
      if (stub) await new Promise<void>((resolve) => stub!.close(() => resolve()))
    })

    async function openTenantAndRun(page: import('@playwright/test').Page, runId: string) {
      await page.goto(RUN_CONTROL_PATH)
      const tenantTile = page.locator('[data-testid="rc-tenant-tile"]').first()
      await expect(tenantTile).toBeVisible({ timeout: 10_000 })
      await tenantTile.locator('button').first().click()
      // Both runs auto-expand (group="running"); scope to the target run's program tile.
      const runTile = page.locator(`[data-testid="rc-program-tile"]:has-text("${runId}")`).first()
      await expect(runTile).toBeVisible({ timeout: 10_000 })
      return runTile
    }

    test('leg a — rerun pre-flight: open owner approval ⇒ warn + disabled submit', async ({
      page,
      fazalJwt,
    }) => {
      test.skip(
        process.env.RC_E2E_STUB_BACKED !== '1',
        'RC_E2E_STUB_BACKED!=1 — needs real Supabase tenants + the stub. See file header.',
      )
      expect(fazalJwt).toBeTruthy()
      const runTile = await openTenantAndRun(page, 'run-overlap-0001')

      // Open the rerun confirm dialog for the open-approval run.
      await runTile.locator('[data-rc-rerun-btn]').first().click()

      // I2 banner is always present in the dialog.
      await expect(page.locator('[data-rc-rerun-i2]')).toContainText(RC_COPY.rerunI2)
      // PRE-FLIGHT re-fetch resolves to open_approval=true → the warn renders + submit disabled.
      await expect(page.locator('[data-rc-rerun-preflight-warn]')).toContainText(
        RC_COPY.preflightWarn,
        { timeout: 10_000 },
      )
      await expect(page.locator('[data-rc-rerun-submit]')).toBeDisabled()
    })

    test('leg b — rerun returns escalated_overlap ⇒ the C1-A disclosure renders', async ({
      page,
      fazalJwt,
    }) => {
      test.skip(
        process.env.RC_E2E_STUB_BACKED !== '1',
        'RC_E2E_STUB_BACKED!=1 — needs real Supabase tenants + the stub. See file header.',
      )
      expect(fazalJwt).toBeTruthy()
      const runTile = await openTenantAndRun(page, 'run-clear-0001')

      await runTile.locator('[data-rc-rerun-btn]').first().click()
      // open_approval=false for this run → submit enables once the pre-flight clears.
      const submit = page.locator('[data-rc-rerun-submit]')
      await expect(submit).toBeEnabled({ timeout: 10_000 })
      await submit.click()

      // The stub returns outcome:'escalated_overlap' → the prominent C1-A disclosure renders.
      await expect(page.locator('[data-rc-rerun-escalated-overlap]')).toContainText(
        RC_COPY.escalatedOverlap,
        { timeout: 10_000 },
      )
    })

    test('override dialog on a controllable step renders the blind-write + consume-fail copy', async ({
      page,
      fazalJwt,
    }) => {
      test.skip(
        process.env.RC_E2E_STUB_BACKED !== '1',
        'RC_E2E_STUB_BACKED!=1 — needs real Supabase tenants + the stub. See file header.',
      )
      expect(fazalJwt).toBeTruthy()
      const runTile = await openTenantAndRun(page, 'run-clear-0001')

      // The controllable step exposes an override button; observed steps would not.
      await runTile.locator('[data-rc-override-btn]').first().click()
      await expect(page.locator('[data-rc-blind-write]')).toContainText(RC_COPY.blindWrite)
      await expect(page.locator('[data-rc-consume-fail]')).toContainText(RC_COPY.consumeFail)
      // The allowed_keys field (limit) renders — a key NAME only, value blank/blind.
      await expect(page.locator('[data-rc-override-key="limit"]')).toBeVisible()
    })
  })
})

// ── T6: VT-376 control copy is in the served JS bundle (stubless — bundle-level) ──
// Mirrors T3: the interactive-control strings are verbatim literals in run-control-controls.tsx
// ('use client'), so they ship in a client chunk even when the controls don't mount in a
// stubless render. This is the bundle-level guarantee for the VT-376 disclosures whose RENDERED
// path needs the stub-backed stack (exercised in T5).
test.describe('VT-376 interactive-control copy in the served JS bundle', () => {
  test('blind-write / consume-fail / I2 / pre-flight / escalated-overlap copy ships in the bundle', async ({
    page,
    request,
    baseURL,
    fazalJwt,
  }) => {
    expect(fazalJwt).toBeTruthy()
    test.skip(!baseURL, 'baseURL required to fetch JS chunks')
    await page.goto(RUN_CONTROL_PATH)
    const html = await page.content()
    const bundle = await collectServedJs(request, baseURL!, html)

    const missing = Object.entries(RC_COPY).filter(([, s]) => !bundle.includes(s))
    expect(
      missing.map(([k]) => k),
      `VT-376 control copy missing from served JS: ${missing.map(([k]) => k).join(', ')}`,
    ).toHaveLength(0)
  })
})
