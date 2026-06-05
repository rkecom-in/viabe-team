/** VT-341 — DSR export + report-download proxy routes. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/auth/require-owner-session', () => {
  class OwnerUnauthorizedError extends Error {}
  return { OwnerUnauthorizedError, requireOwnerSession: vi.fn() }
})

import { POST as exportPOST } from '@/app/api/dsr/export/route'
import { GET as downloadGET } from '@/app/api/team/reports/[year_month]/download/route'
import { OwnerUnauthorizedError, requireOwnerSession } from '@/lib/auth/require-owner-session'

const _req = (init?: RequestInit) =>
  new Request('http://test/x', init) as unknown as Parameters<typeof exportPOST>[0]

beforeEach(() => {
  process.env.TEAM_ORCHESTRATOR_URL = 'http://orch:8001'
  process.env.INTERNAL_API_SECRET = 'sek'
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('VT-341 — POST /api/dsr/export', () => {
  it('401 when no owner session', async () => {
    vi.mocked(requireOwnerSession).mockRejectedValue(new OwnerUnauthorizedError('no'))
    const res = await exportPOST(_req({ method: 'POST' }))
    expect(res.status).toBe(401)
  })

  it('403 on a cross-origin POST (CSRF belt)', async () => {
    vi.mocked(requireOwnerSession).mockResolvedValue({ tenantId: 'tid-1' })
    const res = await exportPOST(_req({ method: 'POST', headers: { origin: 'http://evil.com', host: 'test' } }))
    expect(res.status).toBe(403)
  })

  it('forwards the SESSION tenant + X-Internal-Secret to admin/dsr/export; returns a ZIP', async () => {
    vi.mocked(requireOwnerSession).mockResolvedValue({ tenantId: 'tid-1' })
    const f = vi.fn(async () => ({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(4) }))
    vi.stubGlobal('fetch', f)
    const res = await exportPOST(_req({ method: 'POST', headers: { origin: 'http://test', host: 'test' } }))
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toBe('application/zip')
    const [url, opts] = f.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://orch:8001/api/orchestrator/admin/dsr/export')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body as string)).toEqual({ tenant_id: 'tid-1' })
    expect((opts.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
  })
})

describe('VT-341 — GET report download', () => {
  const _params = (ym: string) => ({ params: Promise.resolve({ year_month: ym }) })

  it('400 on an invalid year_month (no traversal) before any session/fetch', async () => {
    const res = await downloadGET(_req(), _params('2026-13/../x'))
    expect(res.status).toBe(400)
  })

  it('401 when no owner session', async () => {
    vi.mocked(requireOwnerSession).mockRejectedValue(new OwnerUnauthorizedError('no'))
    const res = await downloadGET(_req(), _params('2026-05'))
    expect(res.status).toBe(401)
  })

  it('forwards session tenant + ym, redirects to the signed URL', async () => {
    vi.mocked(requireOwnerSession).mockResolvedValue({ tenantId: 'tid-1' })
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ signed_url: 'https://signed/x' }) }))
    vi.stubGlobal('fetch', f)
    const res = await downloadGET(_req(), _params('2026-05'))
    expect(res.headers.get('location')).toBe('https://signed/x')
    const [url, opts] = f.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://orch:8001/api/orchestrator/owner/report-download-url')
    expect(JSON.parse(opts.body as string)).toEqual({ tenant_id: 'tid-1', year_month: '2026-05' })
  })

  it('404 when the orchestrator has no PDF', async () => {
    vi.mocked(requireOwnerSession).mockResolvedValue({ tenantId: 'tid-1' })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })))
    const res = await downloadGET(_req(), _params('2026-05'))
    expect(res.status).toBe(404)
  })
})
