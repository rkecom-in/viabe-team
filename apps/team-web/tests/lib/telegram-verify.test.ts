/** VT-297 — /link verification: single-use code, takeover guard, fail-closed. */

import { describe, expect, it } from 'vitest'

import { linkTelegram } from '@/lib/telegram/verify'

/** Fake client: update().eq().is().select() resolves to {data, error}. Records the patch. */
function client(result: { data?: any; error?: any }) {
  const calls: any = { patch: null, filters: {} }
  const b: any = {
    update: (patch: any) => {
      calls.patch = patch
      return b
    },
    eq: (c: string, v: unknown) => {
      calls.filters[c] = v
      return b
    },
    is: (c: string, v: unknown) => {
      calls.filters[`${c}__is`] = v
      return b
    },
    select: async () => result,
  }
  return { from: () => b, _calls: calls }
}

describe('VT-297 — linkTelegram', () => {
  it('valid unused code → links (binds telegram_user_id + verified_at, clears code)', async () => {
    const c = client({ data: [{ operator_id: 'op1' }], error: null })
    const r = await linkTelegram(555, 777, 'CODE123', c as never)
    expect(r.ok).toBe(true)
    // single-use: only matches an UNVERIFIED row + clears the code on success
    expect((c as any)._calls.filters['verified_at__is']).toBeNull()
    expect((c as any)._calls.patch.verification_code).toBeNull()
    expect((c as any)._calls.patch.telegram_user_id).toBe(555)
  })

  it('bad/used code → no row updated → bad_code', async () => {
    const c = client({ data: [], error: null })
    const r = await linkTelegram(555, 777, 'WRONG', c as never)
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('bad_code')
  })

  it('telegram account already verified to another operator → already_linked (takeover guard)', async () => {
    const c = client({ data: null, error: { message: 'duplicate key value violates unique constraint' } })
    const r = await linkTelegram(555, 777, 'CODE123', c as never)
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('already_linked')
  })

  it('missing code/user/chat → bad_code, no DB call', async () => {
    expect((await linkTelegram(0, 0, '', client({ data: [] }) as never)).reason).toBe('bad_code')
  })

  it('db error → fail-closed error', async () => {
    const c = client({ data: null, error: { message: 'connection reset' } })
    const r = await linkTelegram(555, 777, 'CODE123', c as never)
    expect(r.ok).toBe(false)
    expect(r.reason).toBe('error')
  })
})
