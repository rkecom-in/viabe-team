/**
 * VT-297 — Telegram inbound identity binding (the IDOR-crux). Fail-closed at every step.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const FAZAL = '00000000-0000-0000-0000-00000000fa2a'

beforeEach(() => {
  vi.stubEnv('FAZAL_OWNER_UUID', FAZAL)
  vi.resetModules()
})
afterEach(() => {
  vi.unstubAllEnvs()
})

/**
 * Programmable client. Per table:
 *  - operator_telegram: select().eq().not().maybeSingle() → telegramRow
 *  - operator_allowlist: select().eq().is().maybeSingle() → allowlistRow
 *  - operator_assignments: select().eq().is() (thenable) → { data: assignmentRows }
 */
function client(opts: {
  telegramRow?: any
  allowlistRow?: any
  assignmentRows?: { tenant_id: string }[]
}) {
  return {
    from(table: string) {
      const b: any = {
        select: () => b,
        eq: () => b,
        is: () => b,
        not: () => b,
        maybeSingle: async () => {
          if (table === 'operator_telegram') return opts.telegramRow ?? { data: null, error: null }
          if (table === 'operator_allowlist') return opts.allowlistRow ?? { data: null, error: null }
          return { data: null, error: null }
        },
        // resolveAssignedTenants awaits the builder after .is()
        then: (resolve: any) =>
          resolve({ data: opts.assignmentRows ?? [], error: null }),
      }
      return b
    },
  }
}

async function resolve(telegramUserId: number | string, c: any) {
  const { resolveOperatorFromTelegram } = await import('@/lib/telegram/identity')
  return resolveOperatorFromTelegram(telegramUserId, c)
}

describe('VT-297 — resolveOperatorFromTelegram (fail-closed)', () => {
  it('no verified binding → null (unknown/spoofed user_id reaches nothing)', async () => {
    const out = await resolve(123, client({ telegramRow: { data: null, error: null } }))
    expect(out).toBeNull()
  })

  it('verified VTR binding → operator + assigned tenants', async () => {
    const out = await resolve(
      123,
      client({
        telegramRow: { data: { operator_id: 'op-vtr', verified_at: '2026-06-03T00:00:00Z' }, error: null },
        allowlistRow: { data: { user_id: 'op-vtr' }, error: null },
        assignmentRows: [{ tenant_id: 'ta' }, { tenant_id: 'tb' }],
      }),
    )
    expect(out).not.toBeNull()
    expect(out!.operatorId).toBe('op-vtr')
    expect(out!.role).toBe('vt_r')
    expect(out!.assignedTenants).toEqual(['ta', 'tb'])
  })

  it('revoked operator → null (TTL=0 revocation, even with a verified binding)', async () => {
    const out = await resolve(
      123,
      client({
        telegramRow: { data: { operator_id: 'op-revoked', verified_at: 'x' }, error: null },
        allowlistRow: { data: null, error: null }, // no active allowlist row
      }),
    )
    expect(out).toBeNull()
  })

  it('Fazal UUID binding → VTAdmin (role from UUID, not a stored column), unscoped', async () => {
    const out = await resolve(
      999,
      client({
        telegramRow: { data: { operator_id: FAZAL, verified_at: 'x' }, error: null },
        // no allowlist row needed — Fazal break-glass
      }),
    )
    expect(out).not.toBeNull()
    expect(out!.role).toBe('vt_admin')
    expect(out!.assignedTenants).toBeNull() // VTAdmin = all tenants
  })

  it('binding query error → null (fail-closed)', async () => {
    const out = await resolve(123, client({ telegramRow: { data: null, error: { message: 'boom' } } }))
    expect(out).toBeNull()
  })

  it('empty/absent user_id → null', async () => {
    expect(await resolve('', client({}))).toBeNull()
  })
})
