/** VT-87 PR-1 — owner-dashboard read client (team-web → orchestrator, X-Internal-Secret). */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  fetchCampaigns,
  fetchCustomers,
  fetchDashboardSummary,
  fetchReports,
  fetchSettings,
} from '@/lib/owner-dashboard-client'

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

const _CUSTOMERS = {
  page: 2,
  page_size: 20,
  total: 41,
  customers: [{ display_name: 'Asha', phone_last4: '••••3210', opt_out_status: 'subscribed', spend_rupees: 500 }],
}

describe('VT-338 — fetchCustomers', () => {
  it('GET with X-Internal-Secret + session tenantId + pagination/filter params', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => _CUSTOMERS }))
    vi.stubGlobal('fetch', f)

    const out = await fetchCustomers('tenant-abc', { page: 2, pageSize: 20, excludedOnly: true })
    expect(out).toEqual(_CUSTOMERS)

    const [url, opts] = f.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(
      'http://orch:8001/api/orchestrator/owner/dashboard-customers' +
        '?tenant_id=tenant-abc&page=2&page_size=20&excluded_only=true',
    )
    expect(opts.method).toBe('GET')
    expect((opts.headers as Record<string, string>)['X-Internal-Secret']).toBe('sek')
  })

  it('defaults page=1, page_size=20, excluded_only=false', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => _CUSTOMERS }))
    vi.stubGlobal('fetch', f)
    await fetchCustomers('t')
    const [url] = f.mock.calls[0] as unknown as [string]
    expect(url).toContain('page=1&page_size=20&excluded_only=false')
  })

  it('returns null on a non-2xx / throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) })))
    expect(await fetchCustomers('t')).toBeNull()
  })
})

describe('VT-338 — fetchCampaigns', () => {
  it('GET with X-Internal-Secret + session tenantId + days_back/limit', async () => {
    const body = { campaigns: [{ campaign_id: 'c1', status: 'sent', template_id: 't', responses: 3, sent_at: null }] }
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
    vi.stubGlobal('fetch', f)
    const out = await fetchCampaigns('tenant-abc', { daysBack: 90, limit: 10 })
    expect(out).toEqual(body)
    const [url] = f.mock.calls[0] as unknown as [string]
    expect(url).toBe(
      'http://orch:8001/api/orchestrator/owner/dashboard-campaigns?tenant_id=tenant-abc&days_back=90&limit=10',
    )
  })

  it('returns null on a non-2xx / throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) })))
    expect(await fetchCampaigns('t')).toBeNull()
  })
})

describe('VT-338 — fetchSettings', () => {
  it('GET with X-Internal-Secret + session tenantId', async () => {
    const body = { business: null, plan: { plan_tier: 'founding', phase: 'active', trial_started_at: null, trial_ends_at: null, trial_extension_count: 0, preferred_language: 'en' } }
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
    vi.stubGlobal('fetch', f)
    const out = await fetchSettings('tenant-abc')
    expect(out).toEqual(body)
    const [url] = f.mock.calls[0] as unknown as [string]
    expect(url).toBe('http://orch:8001/api/orchestrator/owner/dashboard-settings?tenant_id=tenant-abc')
  })

  it('returns null on a non-2xx / throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })))
    expect(await fetchSettings('t')).toBeNull()
  })
})

describe('VT-338 — fetchReports', () => {
  it('GET with X-Internal-Secret + session tenantId', async () => {
    const body = { reports: [{ year_month: '2026-05', generated_at: null, has_pdf: true }] }
    const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
    vi.stubGlobal('fetch', f)
    const out = await fetchReports('tenant-abc')
    expect(out).toEqual(body)
    const [url] = f.mock.calls[0] as unknown as [string]
    expect(url).toBe('http://orch:8001/api/orchestrator/owner/dashboard-reports?tenant_id=tenant-abc')
  })

  it('returns null on a non-2xx / throw', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) })))
    expect(await fetchReports('t')).toBeNull()
  })
})
