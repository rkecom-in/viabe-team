/** VT-297 — inbound webhook handler: secret-gate, replay dedup, identity-gate, dispatch. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FAZAL = '00000000-0000-0000-0000-00000000fa2a'

beforeEach(() => {
  vi.stubEnv('FAZAL_OWNER_UUID', FAZAL)
  vi.resetModules()
})
afterEach(() => vi.unstubAllEnvs())

/**
 * Fake client. Tables:
 *  - telegram_update_replay: insert() → { error } (dupSet controls duplicate)
 *  - operator_telegram: select/eq/not/maybeSingle (binding) OR update/eq/is/select (/link)
 *  - operator_allowlist: select/eq/is/maybeSingle (revocation)
 *  - operator_assignments: select/eq/is (thenable)
 */
function makeClient(opts: {
  seenUpdateIds?: Set<number>
  telegramRow?: any
  allowlistRow?: any
  assignmentRows?: { tenant_id: string }[]
  linkResult?: { data?: any; error?: any }
}) {
  const seen = opts.seenUpdateIds ?? new Set<number>()
  return {
    from(table: string) {
      if (table === 'telegram_update_replay') {
        return {
          insert: async (row: { update_id: number }) => {
            if (seen.has(row.update_id)) return { error: { message: 'duplicate key' } }
            seen.add(row.update_id)
            return { error: null }
          },
        }
      }
      const b: any = {
        select: () => b,
        update: () => b,
        eq: () => b,
        is: () => b,
        not: () => b,
        maybeSingle: async () =>
          table === 'operator_telegram'
            ? opts.telegramRow ?? { data: null, error: null }
            : opts.allowlistRow ?? { data: null, error: null },
        then: (resolve: any) => resolve({ data: opts.assignmentRows ?? [], error: null }),
      }
      // /link path uses update().eq().is().select() (no maybeSingle)
      if (table === 'operator_telegram' && opts.linkResult) {
        b.select = async () => opts.linkResult
      }
      return b
    },
  }
}

async function handle(update: any, client: any) {
  const { handleUpdate } = await import('@/lib/telegram/webhook-handler')
  return handleUpdate(update, client)
}

describe('VT-297 — verifyWebhookSecret (fail-closed)', () => {
  it('unset env → false even if a token is provided', async () => {
    vi.stubEnv('TELEGRAM_OPS_WEBHOOK_SECRET', '')
    const { verifyWebhookSecret } = await import('@/lib/telegram/webhook-handler')
    expect(verifyWebhookSecret('anything')).toBe(false)
  })
  it('match → true; mismatch → false', async () => {
    vi.stubEnv('TELEGRAM_OPS_WEBHOOK_SECRET', 'sekret')
    const { verifyWebhookSecret } = await import('@/lib/telegram/webhook-handler')
    expect(verifyWebhookSecret('sekret')).toBe(true)
    expect(verifyWebhookSecret('nope')).toBe(false)
    expect(verifyWebhookSecret(null)).toBe(false)
  })
})

describe('VT-297 — handleUpdate', () => {
  const msg = (text: string, userId = 42, chatId = 99, update_id = 1) => ({
    update_id,
    message: { text, from: { id: userId }, chat: { id: chatId } },
  })

  it('duplicate update_id → no-op (null), acts at most once', async () => {
    const seen = new Set<number>()
    const c = makeClient({ seenUpdateIds: seen, telegramRow: { data: null, error: null } })
    const first = await handle(msg('/help', 42, 99, 7), c)
    const second = await handle(msg('/help', 42, 99, 7), c) // same update_id
    expect(first).not.toBeNull()
    expect(second).toBeNull()
  })

  it('unresolved user (no verified binding) → not authorized, reaches no data', async () => {
    const c = makeClient({ telegramRow: { data: null, error: null } })
    const out = await handle(msg('/alerts'), c)
    expect(out).toContain('Not authorized')
  })

  it('/link with code → links', async () => {
    const c = makeClient({ linkResult: { data: [{ operator_id: 'op1' }], error: null } })
    const out = await handle(msg('/link CODE123'), c)
    expect(out).toContain('Linked')
  })

  it('/link bad code → invalid', async () => {
    const c = makeClient({ linkResult: { data: [], error: null } })
    const out = await handle(msg('/link WRONG'), c)
    expect(out).toContain('Invalid')
  })

  it('verified VTR + /help → help reply', async () => {
    const c = makeClient({
      telegramRow: { data: { operator_id: 'op-vtr', verified_at: 'x' }, error: null },
      allowlistRow: { data: { user_id: 'op-vtr' }, error: null },
      assignmentRows: [{ tenant_id: 'ta' }],
    })
    const out = await handle(msg('/help'), c)
    expect(out).toContain('/alerts')
  })

  it('malformed update (no message) → null', async () => {
    const c = makeClient({})
    expect(await handle({ update_id: 5 }, c)).toBeNull()
  })
})
