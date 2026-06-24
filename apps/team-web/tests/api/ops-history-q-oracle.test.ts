/**
 * VT-412 PR-D (adversarial-review Finding 1) — the `q` free-text search ORACLE.
 *
 * GET /api/ops/history?q=... runs `.textSearch('envelope_search_tsv', q)` against a
 * tsvector built from RAW input_envelope || output_envelope text (migrations/038),
 * BEFORE de-identification (de-id is applied to result ROWS after the query). So a
 * VTR could use result-set MEMBERSHIP as an oracle (q=<customer-name> / q=<phone>
 * ⇒ rows ⇒ "that token is present in my assigned tenants' raw data") even though
 * the returned rows are de-identified.
 *
 * Fix proof: for a VTR (operator.assignedTenants !== null) the route DROPS `q`
 * before it reaches fetchHistoricalSteps → no textSearch on the raw tsv → no oracle.
 * VTAdmin / Fazal (assignedTenants === null) KEEP `q`. The role is resolved
 * server-side from the gated operator object — never a client flag. Tenant-scoping
 * and row de-id are unaffected.
 *
 * We assert on the `q` ACTUALLY PASSED to fetchHistoricalSteps (the mock captures
 * the call args) — that is the value that reaches `.textSearch`. If `q` is undefined
 * the textSearch branch in data-access is never taken, so there is nothing to oracle.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const TENANT_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const FAZAL_PHONE_PROBE = '+919876543210'

// A captured call into fetchHistoricalSteps — we only care about `q` + tenantIds.
type FetchArgs = {
  q?: string
  tenantIds?: string[]
  date: string
}

async function callHistory(opts: {
  assignedTenants: string[] | null
  url: string
}): Promise<{ status: number; fetchArgs: FetchArgs | undefined }> {
  const fetchHistoricalSteps = vi
    .fn<(a: FetchArgs) => Promise<{ rows: unknown[]; nextCursor: string | null }>>()
    .mockResolvedValue({ rows: [], nextCursor: null })

  // requireOpsOperator resolves a GATED operator; the role (VTR vs VTAdmin) is the
  // assignedTenants shape — null = VTAdmin/Fazal, string[] = VTR. We never read a
  // client flag for it.
  vi.doMock('@/lib/auth/require-ops-operator', () => ({
    requireOpsOperator: vi
      .fn()
      .mockResolvedValue({ id: 'op-1', assignedTenants: opts.assignedTenants }),
  }))
  vi.doMock('@/lib/ops/data-access', () => ({ fetchHistoricalSteps }))

  const { GET } = await import('@/app/api/ops/history/route')
  const req = new Request(opts.url)
  // The route reads req.nextUrl; NextRequest wraps a Request and adds it.
  const { NextRequest } = await import('next/server')
  const res = await GET(new NextRequest(req))

  const call = fetchHistoricalSteps.mock.calls[0]
  return { status: res.status, fetchArgs: call ? call[0] : undefined }
}

describe('VT-412 PR-D — q-search oracle is closed for a VTR, kept for VTAdmin', () => {
  beforeEach(() => {
    vi.resetModules()
  })
  afterEach(() => {
    vi.resetModules()
    vi.restoreAllMocks()
  })

  it('VTR: `q` is DROPPED before fetchHistoricalSteps (no textSearch on raw tsv → no oracle)', async () => {
    const { fetchArgs } = await callHistory({
      assignedTenants: [TENANT_A],
      url: `http://test/api/ops/history?date=2026-06-24&q=${encodeURIComponent(FAZAL_PHONE_PROBE)}`,
    })
    expect(fetchArgs).toBeDefined()
    // The oracle probe never reaches the query path.
    expect(fetchArgs?.q).toBeUndefined()
    // Tenant-scoping is still applied (the VTR's assigned set), unaffected by the q drop.
    expect(fetchArgs?.tenantIds).toEqual([TENANT_A])
  })

  it('VTR: a name probe is ALSO dropped (not just phone) — structural, any q', async () => {
    const { fetchArgs } = await callHistory({
      assignedTenants: [TENANT_A],
      url: 'http://test/api/ops/history?date=2026-06-24&q=Lakshmi',
    })
    expect(fetchArgs?.q).toBeUndefined()
  })

  it('VTAdmin / Fazal (assignedTenants null): `q` is PRESERVED — full search retained', async () => {
    const { fetchArgs } = await callHistory({
      assignedTenants: null,
      url: `http://test/api/ops/history?date=2026-06-24&q=${encodeURIComponent(FAZAL_PHONE_PROBE)}`,
    })
    expect(fetchArgs).toBeDefined()
    expect(fetchArgs?.q).toBe(FAZAL_PHONE_PROBE)
    // VTAdmin filter passes through unscoped (undefined = all).
    expect(fetchArgs?.tenantIds).toBeUndefined()
  })

  it('VTAdmin: absent `q` stays undefined (no spurious empty search)', async () => {
    const { fetchArgs } = await callHistory({
      assignedTenants: null,
      url: 'http://test/api/ops/history?date=2026-06-24',
    })
    expect(fetchArgs?.q).toBeUndefined()
  })
})
