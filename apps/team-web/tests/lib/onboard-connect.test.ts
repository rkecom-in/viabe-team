/** VT-267 PR-C — connect/handoff + connection-status resume (server-side). */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { checkConnection, startConnect } from '@/lib/onboard/connect'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function _fetchOnce(status: number, body: unknown) {
  const f = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }))
  vi.stubGlobal('fetch', f)
  return f
}

describe('VT-267 PR-C — startConnect (system-browser handoff URL)', () => {
  it('google_sheet → returns the orchestrator auth_url', async () => {
    _fetchOnce(200, { auth_url: 'https://accounts.google.com/o/oauth2/v2/auth?x=1' })
    const r = await startConnect('t1', 'google_sheet')
    expect(r.ok).toBe(true)
    expect(r.authUrl).toContain('accounts.google.com')
  })

  it('whatsapp → returns the embedded_signup_url (different key)', async () => {
    _fetchOnce(200, { embedded_signup_url: 'https://www.facebook.com/embedded_signup?x=1' })
    const r = await startConnect('t1', 'whatsapp')
    expect(r.ok).toBe(true)
    expect(r.authUrl).toContain('facebook.com')
  })

  it('shopify → returns the authorize_url (VT-422: NOT auth_url — endpoint key)', async () => {
    // The shopify /setup endpoint returns `authorize_url`; using `auth_url` would
    // silently null the URL. This locks the correct key.
    const f = _fetchOnce(200, { authorize_url: 'https://shop.myshopify.com/admin/oauth/authorize?x=1' })
    const r = await startConnect('t1', 'shopify', 'shop.myshopify.com')
    expect(r.ok).toBe(true)
    expect(r.authUrl).toContain('myshopify.com/admin/oauth/authorize')
    // the shop domain is threaded into the /setup body.
    const calls = f.mock.calls as unknown as Array<[string, { body: string }]>
    const body = JSON.parse(calls[0]![1].body)
    expect(body).toEqual({ tenant_id: 't1', shop: 'shop.myshopify.com' })
  })

  it('shopify → wrong key (auth_url) yields no url (proves the authorize_url requirement)', async () => {
    _fetchOnce(200, { auth_url: 'https://shop.myshopify.com/admin/oauth/authorize?x=1' })
    const r = await startConnect('t1', 'shopify', 'shop.myshopify.com')
    expect(r.ok).toBe(false)
    expect(r.authUrl).toBeNull()
  })

  it('shopify → missing shop domain → missing_shop, no fetch', async () => {
    const f = _fetchOnce(200, { authorize_url: 'x' })
    const r = await startConnect('t1', 'shopify')
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('missing_shop')
    expect(f).not.toHaveBeenCalled()
  })

  it('non-2xx → fail with http_<n>, no url', async () => {
    _fetchOnce(500, {})
    const r = await startConnect('t1', 'google_sheet')
    expect(r.ok).toBe(false)
    expect(r.authUrl).toBeNull()
    expect(r.reason).toBe('http_500')
  })

  it('missing url in body → error (never returns a bad url)', async () => {
    _fetchOnce(200, { wrong_key: 'x' })
    const r = await startConnect('t1', 'google_sheet')
    expect(r.ok).toBe(false)
    expect(r.authUrl).toBeNull()
  })

  it('no tenant → misconfig, no fetch', async () => {
    const f = _fetchOnce(200, { auth_url: 'x' })
    const r = await startConnect('', 'google_sheet')
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('misconfig')
    expect(f).not.toHaveBeenCalled()
  })
})

describe('VT-267 PR-C — checkConnection (resume signal, fail-closed)', () => {
  function client(table: string, row: any) {
    const builder: any = {
      select: () => builder,
      eq: () => builder,
      maybeSingle: async () => row,
    }
    return { from: (t: string) => (t === table ? builder : { from: () => builder, select: () => builder, eq: () => builder, maybeSingle: async () => ({ data: null }) }) }
  }

  it('google_sheet connected when row enabled', async () => {
    const c = client('tenant_connector_status', { data: { enabled: true, last_status: 'ok' }, error: null })
    const s = await checkConnection('t1', 'google_sheet', c as never)
    expect(s.connected).toBe(true)
    expect(s.detail).toBe('ok')
  })

  it('google_sheet NOT connected when no row (fail-closed)', async () => {
    const c = client('tenant_connector_status', { data: null, error: null })
    const s = await checkConnection('t1', 'google_sheet', c as never)
    expect(s.connected).toBe(false)
  })

  it('whatsapp connected once status past pending', async () => {
    const c = client('tenant_whatsapp_accounts', { data: { status: 'verifying' }, error: null })
    const s = await checkConnection('t1', 'whatsapp', c as never)
    expect(s.connected).toBe(true)
    expect(s.detail).toBe('verifying')
  })

  it('shopify connected once a tenant_oauth_tokens row exists (VT-422 GAP-3)', async () => {
    const c = client('tenant_oauth_tokens', { data: { shop_url: 'shop.myshopify.com' }, error: null })
    const s = await checkConnection('t1', 'shopify', c as never)
    expect(s.connected).toBe(true)
    expect(s.detail).toBe('shop.myshopify.com')
  })

  it('shopify NOT connected when no token row (fail-closed)', async () => {
    const c = client('tenant_oauth_tokens', { data: null, error: null })
    const s = await checkConnection('t1', 'shopify', c as never)
    expect(s.connected).toBe(false)
  })

  it('whatsapp NOT connected while pending', async () => {
    const c = client('tenant_whatsapp_accounts', { data: { status: 'pending' }, error: null })
    const s = await checkConnection('t1', 'whatsapp', c as never)
    expect(s.connected).toBe(false)
  })

  it('error → fail-closed not connected', async () => {
    const c = client('tenant_connector_status', { data: null, error: { message: 'boom' } })
    const s = await checkConnection('t1', 'google_sheet', c as never)
    expect(s.connected).toBe(false)
    expect(s.detail).toBe('not connected')
  })

  it('no tenant → not connected', async () => {
    const s = await checkConnection('', 'whatsapp', { from: () => ({}) } as never)
    expect(s.connected).toBe(false)
  })
})
