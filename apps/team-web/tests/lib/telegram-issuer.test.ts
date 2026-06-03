/** VT-297 — link-code issuer: server-side code mint onto the caller's own row. */

import { describe, expect, it } from 'vitest'

import { generateLinkCode, mintLinkCode } from '@/lib/telegram/issuer'

describe('VT-297 — generateLinkCode', () => {
  it('produces a non-trivial hex code; distinct across calls', () => {
    const a = generateLinkCode()
    const b = generateLinkCode()
    expect(a).toMatch(/^[0-9A-F]{10}$/)
    expect(a).not.toBe(b)
  })
})

describe('VT-297 — mintLinkCode', () => {
  function client(result: { error?: any }) {
    const calls: any = { row: null, onConflict: null }
    return {
      _calls: calls,
      from: () => ({
        upsert: async (row: any, opts: any) => {
          calls.row = row
          calls.onConflict = opts?.onConflict
          return result
        },
      }),
    }
  }

  it('mints a code onto the operator row, resets verification, conflict on operator_id', async () => {
    const c = client({ error: null })
    const r = await mintLinkCode('op-1', c as never)
    expect(r.ok).toBe(true)
    expect(r.code).toMatch(/^[0-9A-F]{10}$/)
    expect((c as any)._calls.row).toMatchObject({
      operator_id: 'op-1',
      verified_at: null, // re-issue resets verification
      telegram_user_id: null,
      verification_code: r.code,
    })
    expect((c as any)._calls.onConflict).toBe('operator_id')
  })

  it('no operator → fail, no code', async () => {
    const r = await mintLinkCode('', client({ error: null }) as never)
    expect(r.ok).toBe(false)
    expect(r.code).toBeNull()
  })

  it('db error → fail-closed', async () => {
    const r = await mintLinkCode('op-1', client({ error: { message: 'boom' } }) as never)
    expect(r.ok).toBe(false)
    expect(r.code).toBeNull()
  })
})
