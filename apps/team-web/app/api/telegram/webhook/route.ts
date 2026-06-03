/**
 * VT-297 — inbound Telegram webhook (POST). Thin HTTP shell over `handleUpdate`.
 *
 * SECURITY ORDER: verify the X-Telegram-Bot-Api-Secret-Token header (constant-time, fail-CLOSED
 * if the env secret is unset) BEFORE parsing the body or touching the DB. A forged update with no
 * valid secret gets 403 and never reaches identity/dispatch. After verify: parse, handle (replay
 * guard → identity → dispatch), best-effort reply, return 200 (Telegram requires a fast 2xx;
 * non-200 triggers re-delivery).
 *
 * setWebhook must be configured with secret_token = TELEGRAM_OPS_WEBHOOK_SECRET (bootstrap step;
 * if the secret is unset, every update 403s — fail-closed, no silent open).
 */

import { NextResponse } from 'next/server'

import { handleUpdate, verifyWebhookSecret } from '@/lib/telegram/webhook-handler'
import { sendTelegramReply } from '@/lib/telegram/send'

export const dynamic = 'force-dynamic'

export async function POST(req: Request): Promise<NextResponse> {
  // 1. Secret FIRST — before body parse / any DB read.
  if (!verifyWebhookSecret(req.headers.get('x-telegram-bot-api-secret-token'))) {
    return NextResponse.json({ ok: false }, { status: 403 })
  }

  let update: unknown
  try {
    update = await req.json()
  } catch {
    return NextResponse.json({ ok: true }, { status: 200 }) // malformed → ack, no work
  }

  let reply: string | null = null
  try {
    reply = await handleUpdate(update as never)
  } catch {
    // Never 5xx (would trigger Telegram re-delivery storms). Swallow + 200.
    return NextResponse.json({ ok: true }, { status: 200 })
  }

  if (reply) {
    const chatId = (update as { message?: { chat?: { id?: number } } })?.message?.chat?.id
    if (chatId !== undefined) await sendTelegramReply(chatId, reply)
  }
  return NextResponse.json({ ok: true }, { status: 200 })
}
