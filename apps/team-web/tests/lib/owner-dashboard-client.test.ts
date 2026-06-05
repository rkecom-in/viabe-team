/** VT-87 PR-1 — owner-dashboard read client (team-web → orchestrator, X-Internal-Secret). */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchDashboardSummary } from '@/lib/owner-dashboard-client'

beforeEach(() => {
  process.env.TEAM_ORCHESTRATOR_URL = 'http://orch:8001'
  process.env.INTERNAL_API_SECRET = 'sek'
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const _SUMMARY = {
  customer_count: 7,
  top_customers: [{ display_name: 'Asha', phone_last4: '••••3210', spend_rupees: 5000 }],
  recent_campaigns: [
    { campaign_id: 'c1', status: 'sent', template_id: 't', responses: 2, sent_at: null },
  ],
}

describe('VT-87 — fetchDashboardSummary', () => {
  it('GET with X-Internal-Secret + session tenantId in the query; returns the summary', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => _SUMMARY }))
    vi.stubGlobal('fetch', f)

    const out = await fetchDashboardSummary('tenant-abc')
    expect(out).toEqual(_SUMMARY)

    const [url, opts] = f.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(
      'http://orch:8001/api/orchestrator/owner/dashboard-summary?tenant_id=tenant-abc',
    )
    expect(opts.method).toBe('GET')
    expect((opts.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
  })

  it('returns null on a non-2xx (e.g. 403 secret mismatch)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })))
    expect(await fetchDashboardSummary('t')).toBeNull()
  })

  it('returns null on a network throw (never leaks an error to the page)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network')
    }))
    expect(await fetchDashboardSummary('t')).toBeNull()
  })
})
