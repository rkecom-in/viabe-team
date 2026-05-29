/**
 * VT-235 — Ops Console styling smoke tests.
 *
 * Source-checks that the styling pass landed Tailwind utility classes
 * across the workspace + subpages. Real visual + responsive verified
 * at playwright e2e + manual canary time.
 */

import { describe, expect, it } from 'vitest'

describe('VT-235 — Ops Console styling', () => {
  const pages = [
    'app/(app)/team/ops/page.tsx',
    'app/(app)/team/ops/stream/page.tsx',
    'app/(app)/team/ops/stream/history/page.tsx',
    'app/(app)/team/ops/runs/[runId]/page.tsx',
    'app/(app)/team/ops/tenants/[tenantId]/page.tsx',
  ]

  it.each(pages)('A1 — %s applies bg-gray-50 + min-h-screen to <main>', async (path) => {
    const { readFile } = await import('fs/promises')
    const pathMod = await import('path')
    const filePath = pathMod.resolve(process.cwd(), path)
    const src = await readFile(filePath, 'utf8')
    expect(src).toContain('bg-gray-50')
    expect(src).toContain('min-h-screen')
  })

  it('A2 — workspace page uses grid + divide-y on tables', async () => {
    const { readFile } = await import('fs/promises')
    const pathMod = await import('path')
    const filePath = pathMod.resolve(
      process.cwd(),
      'app/(app)/team/ops/page.tsx',
    )
    const src = await readFile(filePath, 'utf8')
    expect(src).toMatch(/grid grid-cols-/)
    expect(src).toMatch(/divide-y/)
  })

  it('A3 — sticky banner uses pill chips + amber baseline', async () => {
    const { readFile } = await import('fs/promises')
    const pathMod = await import('path')
    const filePath = pathMod.resolve(
      process.cwd(),
      'components/ops/sticky-banner.tsx',
    )
    const src = await readFile(filePath, 'utf8')
    expect(src).toContain('bg-amber-50')
    // Red intensification path present
    expect(src).toContain('bg-red-50')
    // Pills flex layout
    expect(src).toMatch(/flex.*gap-/)
  })
})
