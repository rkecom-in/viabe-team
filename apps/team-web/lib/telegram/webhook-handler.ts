/**
 * VT-297 — inbound Telegram webhook handler (the gatekeeper). Separated from the HTTP route so
 * the security order is unit-testable.
 *
 * Order (load-bearing):
 *  1. SECRET FIRST — the route verifies X-Telegram-Bot-Api-Secret-Token BEFORE calling this
 *     handler (constant-time; fail-CLOSED if the env secret is unset). No body parse, no DB read
 *     happens on an unverified request.
 *  2. REPLAY — atomic INSERT of update_id into telegram_update_replay; a duplicate is a no-op
 *     (Telegram re-delivers on slow 200s; a captured update can't be replayed for /link or actions).
 *  3. IDENTITY — every non-/link command resolves the operator via resolveOperatorFromTelegram
 *     (verified binding → active operator → scope), fail-closed. An unresolved user gets a generic
 *     "not authorized" reply and reaches NO tenant data.
 *  4. DISPATCH — /link verifies the code; read commands run the scoped/de-identified ops reads.
 *
 * Returns the reply text (or null for a no-op / silent). Never throws.
 */

import { timingSafeEqual } from 'node:crypto'

import { serverSecretClient } from '@/lib/supabase-client'

import { actByReference } from '@/lib/telegram/actions'
import { dispatchReadCommand, parseCommand } from '@/lib/telegram/commands'
import { resolveOperatorFromTelegram } from '@/lib/telegram/identity'
import { escapeHtml } from '@/lib/telegram/send'
import { linkTelegram } from '@/lib/telegram/verify'

type Client = { from: (t: string) => any }

/** Constant-time string compare. Fail-closed: empty expected (env unset) → false. */
export function verifyWebhookSecret(provided: string | null | undefined): boolean {
  const expected = (process.env.TELEGRAM_OPS_WEBHOOK_SECRET ?? '').trim()
  if (!expected || !provided) return false
  const a = Buffer.from(provided)
  const b = Buffer.from(expected)
  if (a.length !== b.length) return false
  try {
    return timingSafeEqual(a, b)
  } catch {
    return false
  }
}

export interface TelegramUpdate {
  update_id?: number
  message?: {
    text?: string
    from?: { id?: number }
    chat?: { id?: number }
  }
}

/** True if this update_id is NEW (inserted); false if a duplicate (replay) or on error
 *  (fail-closed: treat an insert error as a duplicate so we don't act twice). */
async function _claimUpdateId(updateId: number, client: Client): Promise<boolean> {
  try {
    const { error } = await client.from('telegram_update_replay').insert({ update_id: updateId })
    return !error // unique-violation (duplicate) or any error → not new
  } catch {
    return false
  }
}

export async function handleUpdate(
  update: TelegramUpdate,
  client: Client = serverSecretClient(),
): Promise<string | null> {
  const msg = update?.message
  const userId = msg?.from?.id
  const chatId = msg?.chat?.id
  const text = msg?.text ?? ''
  const updateId = update?.update_id

  if (typeof updateId !== 'number' || userId === undefined || chatId === undefined) return null

  // 2. Replay guard — act at most once per update_id.
  if (!(await _claimUpdateId(updateId, client))) return null

  const parsed = parseCommand(text)

  // 4a. /link — single-use code → bind this telegram_user_id. No prior identity needed (that's
  //     the whole point of linking), but the code is the secret + resolves the operator server-side.
  if (parsed.cmd === '/link') {
    const code = parsed.args[0] ?? ''
    const res = await linkTelegram(userId, chatId, code, client)
    if (res.ok) return escapeHtml('Linked ✓ — your Telegram is now connected. Try /help.')
    if (res.reason === 'already_linked') {
      return escapeHtml('That Telegram account is already linked to another operator.')
    }
    return escapeHtml('Invalid or expired code. Get a fresh code from the web console, then /link <code>.')
  }

  // 3. Identity — every other command requires a verified, active operator. Fail-closed.
  const operator = await resolveOperatorFromTelegram(userId, client)
  if (!operator) {
    return escapeHtml('Not authorized. Open the Viabe ops console, generate a link code, then send /link <code> here.')
  }

  // 4b. Mutating actions (/ack, /resolve) — IDOR-safe: resolve the escalation within the
  //     operator's OWN scoped set, act on the server-resolved tenant/id, audit (see actions.ts).
  if (parsed.cmd === '/ack' || parsed.cmd === '/resolve') {
    const action = parsed.cmd === '/ack' ? 'ack' : 'resolve'
    return actByReference(operator, parsed.args[0] ?? '', action, client)
  }

  // 4c. Read commands (scoped + de-identified).
  return dispatchReadCommand(parsed, operator, client)
}
