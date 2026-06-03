/**
 * VT-297 — /link verification: bind a Telegram account to a VTR via a single-use code.
 *
 * The VTR-authed web issuer page mints a one-time `verification_code` onto the operator's
 * operator_telegram row (operator_id known, telegram_user_id/verified_at NULL). The VTR then
 * sends `/link <code>` from Telegram; this binds the inbound telegram_user_id + chat_id to that
 * row and stamps verified_at.
 *
 * Security:
 *  - SINGLE-USE: matched only on a row with verified_at IS NULL; on success verification_code is
 *    cleared, so the code cannot be reused.
 *  - TAKEOVER GUARD: the partial-unique index (mig 076, verified telegram_user_id) makes the
 *    UPDATE fail if this telegram account is already verified to a DIFFERENT operator → linkTelegram
 *    returns false (caught), never silently re-binds.
 *  - operator_id is resolved FROM the code's row server-side — the Telegram user never names an
 *    operator (no open enrollment).
 * Never throws.
 */

import { serverSecretClient } from '@/lib/supabase-client'

type Client = { from: (t: string) => any }

export interface LinkResult {
  ok: boolean
  /** ok | bad_code | already_linked | error */
  reason: string
}

export async function linkTelegram(
  telegramUserId: number | string,
  chatId: number | string,
  code: string,
  client: Client = serverSecretClient(),
): Promise<LinkResult> {
  const trimmed = (code ?? '').trim()
  if (!telegramUserId || !chatId || !trimmed) return { ok: false, reason: 'bad_code' }
  try {
    // Single-use: only an UNVERIFIED row carrying this exact code matches. Atomic UPDATE +
    // RETURNING — no read-then-write race.
    const { data, error } = await client
      .from('operator_telegram')
      .update({
        telegram_user_id: telegramUserId,
        chat_id: String(chatId),
        verified_at: new Date().toISOString(),
        verification_code: null,
      })
      .eq('verification_code', trimmed)
      .is('verified_at', null)
      .select('operator_id')

    if (error) {
      // A unique-violation here = this telegram account is already verified to another operator
      // (the partial-unique takeover guard). Fail-closed, no re-bind.
      const msg = String(error.message ?? error).toLowerCase()
      if (msg.includes('unique') || msg.includes('duplicate')) return { ok: false, reason: 'already_linked' }
      return { ok: false, reason: 'error' }
    }
    const rows = (data ?? []) as { operator_id: string }[]
    if (rows.length === 0) return { ok: false, reason: 'bad_code' } // bad / used / expired code
    return { ok: true, reason: 'ok' }
  } catch {
    return { ok: false, reason: 'error' }
  }
}
