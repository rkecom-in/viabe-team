/**
 * VT-297 — link-code issuer (the precondition that closes the open-enrollment hole).
 *
 * A VTR-authed web action mints a single-use code onto THEIR OWN operator_telegram row
 * (operator_id from the authenticated session — never a client field). `/link <code>` from
 * Telegram then matches the code (verify.ts) and binds the inbound telegram_user_id. Without this
 * issuer, /link could never succeed (or an implementer would let Telegram self-enroll — the hole
 * the adversarial review flagged).
 *
 * Re-issuing resets verification (verified_at + telegram_user_id cleared) so a fresh code always
 * re-binds explicitly. chat_id starts as '' (mig 075 NOT NULL placeholder); /link fills the real
 * one. operator_telegram is deny-all RLS → serverSecretClient. CL-390: no PII.
 */

import { randomBytes } from 'node:crypto'

import { serverSecretClient } from '@/lib/supabase-client'

type Client = { from: (t: string) => any }

/** 10-hex-char single-use code (server-side entropy). */
export function generateLinkCode(): string {
  return randomBytes(5).toString('hex').toUpperCase()
}

export interface MintResult {
  ok: boolean
  code: string | null
  reason: string
}

export async function mintLinkCode(
  operatorId: string,
  client: Client = serverSecretClient(),
): Promise<MintResult> {
  if (!operatorId) return { ok: false, code: null, reason: 'no_operator' }
  const code = generateLinkCode()
  try {
    const { error } = await client.from('operator_telegram').upsert(
      {
        operator_id: operatorId,
        chat_id: '', // placeholder until /link binds the real chat
        verification_code: code,
        verified_at: null, // re-issuing resets verification — explicit re-link
        telegram_user_id: null,
      },
      { onConflict: 'operator_id' },
    )
    if (error) return { ok: false, code: null, reason: String(error.message ?? error) }
    return { ok: true, code, reason: 'ok' }
  } catch (err) {
    return { ok: false, code: null, reason: err instanceof Error ? err.message : 'error' }
  }
}
