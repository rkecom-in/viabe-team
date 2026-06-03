/**
 * VT-297 — Telegram reply send (outbound, from team-web) + HTML escaping.
 *
 * Replies use parse_mode=HTML, so EVERY interpolated value (tenant business_name, statuses,
 * detector kinds) MUST be HTML-escaped before send — the de-identify layer strips PII but does
 * NOT escape markup, and business_name is owner-controlled data (could break formatting / inject).
 * CL-390: no raw phone/customer fields ever reach here (the read fns mask them).
 *
 * Never throws (the webhook must return 200 fast regardless of reply success).
 */

const _ORCHESTRATOR_TELEGRAM_API = 'https://api.telegram.org'
const _SEND_TIMEOUT_MS = 10_000
/** Telegram hard message cap; truncate on a safe boundary so an HTML tag is never split. */
const _MAX_LEN = 3900

/** Escape the 5 HTML-significant chars Telegram's HTML parse_mode cares about. */
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** Truncate to the Telegram cap WITHOUT cutting mid-entity (truncate the already-escaped text on
 *  a whitespace boundary, append an ellipsis). */
export function clampMessage(text: string): string {
  if (text.length <= _MAX_LEN) return text
  const cut = text.slice(0, _MAX_LEN)
  const lastSpace = cut.lastIndexOf('\n') > 0 ? cut.lastIndexOf('\n') : cut.lastIndexOf(' ')
  return `${cut.slice(0, lastSpace > 0 ? lastSpace : _MAX_LEN)}\n…(truncated)`
}

export async function sendTelegramReply(chatId: string | number, text: string): Promise<boolean> {
  const token = process.env.TELEGRAM_OPS_BOT_TOKEN ?? ''
  if (!token || !chatId) return false
  try {
    const res = await fetch(`${_ORCHESTRATOR_TELEGRAM_API}/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text: clampMessage(text), parse_mode: 'HTML' }),
      signal: AbortSignal.timeout(_SEND_TIMEOUT_MS),
    })
    return res.ok
  } catch {
    return false
  }
}
