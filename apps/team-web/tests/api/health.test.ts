import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GET } from '@/app/api/team/health/route'

const originalEnv = process.env.NEXT_PUBLIC_SUPABASE_URL

describe('GET /api/team/health (VT-219)', () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_SUPABASE_URL
  })

  afterEach(() => {
    if (originalEnv === undefined) {
      delete process.env.NEXT_PUBLIC_SUPABASE_URL
    } else {
      process.env.NEXT_PUBLIC_SUPABASE_URL = originalEnv
    }
    vi.restoreAllMocks()
  })

  it('reads NEXT_PUBLIC_SUPABASE_URL (no TEAM_ prefix) — 503 when unset', async () => {
    const res = await GET()
    expect(res.status).toBe(503)
    const body = (await res.json()) as { reason: string }
    expect(body.reason).toBe('NEXT_PUBLIC_SUPABASE_URL not set')
  })

  it('200 when env set and Supabase /auth/v1/health returns ok', async () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://example.supabase.co'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('ok', { status: 200 })),
    )
    const res = await GET()
    expect(res.status).toBe(200)
  })

  it('503 when Supabase /auth/v1/health returns non-2xx', async () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://example.supabase.co'
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('boom', { status: 500 })),
    )
    const res = await GET()
    expect(res.status).toBe(503)
  })
})
