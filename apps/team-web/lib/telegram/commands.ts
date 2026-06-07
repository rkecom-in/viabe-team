/**
 * VT-297 — Telegram VTR bot command router (read surface).
 *
 * Every read command receives the SERVER-RESOLVED operator (from resolveOperatorFromTelegram) —
 * NEVER a chat-supplied operator/tenant field — and runs through the EXISTING scoped +
 * de-identified ops reads (fetchEscalations / fetchMonitoringBoard / fetchActiveRuns), which
 * fail-closed to [] for an unassigned VTR. Output is HTML-escaped (parse_mode=HTML) and carries
 * no PII (the reads mask it; CL-426/CL-390).
 *
 * Mutating actions (/ack /resolve) + /link verification are handled in the webhook/verify layer
 * (each re-derives tenant + audits). This module is the read parity surface.
 */

import { fetchActiveRuns } from '@/lib/ops/activity'
import { fetchEscalations } from '@/lib/ops/escalations'
import { fetchMonitoringBoard } from '@/lib/ops/monitoring'
import { OperatorRole } from '@/lib/auth/roles'
import { escapeHtml } from '@/lib/telegram/send'

type Client = { from: (t: string) => any }

export interface BotOperator {
  operatorId: string
  role: OperatorRole
  assignedTenants: string[] | null
}

export interface ParsedCommand {
  cmd: string
  args: string[]
}

/** Parse "/cmd arg1 arg2" → {cmd:'/cmd', args:[...]}. Non-command text → {cmd:'', args}. */
export function parseCommand(text: string): ParsedCommand {
  const trimmed = (text ?? '').trim()
  const parts = trimmed.split(/\s+/)
  const head = parts[0] ?? ''
  if (head.startsWith('/')) return { cmd: head.toLowerCase(), args: parts.slice(1) }
  return { cmd: '', args: parts }
}

const HELP = [
  'Viabe ops bot. Commands:',
  '/alerts — open escalations for your businesses',
  '/status — watchdog health (crash/stall/misbehaviour)',
  '/runs — recent agent runs',
  '/ack <id> — acknowledge an escalation (tap-to-copy the id from /alerts)',
  '/resolve <id> — resolve an escalation (tap-to-copy the id from /alerts)',
  '/help — this message',
].join('\n')

function _scope(op: BotOperator): string {
  return op.assignedTenants === null ? 'all businesses' : 'your businesses'
}

/** Dispatch a READ command. Returns the (HTML-safe) reply text. Unknown → help. */
export async function dispatchReadCommand(
  parsed: ParsedCommand,
  op: BotOperator,
  client?: Client,
): Promise<string> {
  switch (parsed.cmd) {
    case '/help':
    case '':
      return escapeHtml(HELP)

    case '/alerts': {
      const rows = await fetchEscalations(op, client as never)
      if (rows.length === 0) return escapeHtml(`No open escalations (${_scope(op)}).`)
      const bySev = rows.reduce<Record<string, number>>((acc, r) => {
        const k = r.severity ?? 'unknown'
        acc[k] = (acc[k] ?? 0) + 1
        return acc
      }, {})
      const head = `<b>Escalations (${escapeHtml(_scope(op))}): ${rows.length}</b>`
      const counts = Object.entries(bySev)
        .map(([s, n]) => `${escapeHtml(s)}: ${n}`)
        .join(', ')
      // VT-360 fork A: render the id (the action handle) in <code> → tap-to-copy on mobile, so
      // /ack /resolve is copy+paste, never hand-typing a UUID.
      const top = rows
        .slice(0, 10)
        .map((r) => `• <code>${escapeHtml(r.reference)}</code> — ${escapeHtml(r.kind ?? '?')} (${escapeHtml(r.severity ?? '?')})`)
        .join('\n')
      return `${head}\n${escapeHtml(counts)}\n${top}`
    }

    case '/status': {
      const items = await fetchMonitoringBoard(op, client as never)
      if (items.length === 0) return escapeHtml(`No watchdog signals in 24h (${_scope(op)}).`)
      const byCat = items.reduce<Record<string, number>>((acc, it) => {
        acc[it.category] = (acc[it.category] ?? 0) + 1
        return acc
      }, {})
      const head = `<b>Watchdog (${escapeHtml(_scope(op))}): ${items.length} in 24h</b>`
      const counts = Object.entries(byCat)
        .map(([c, n]) => `${escapeHtml(c)}: ${n}`)
        .join(', ')
      return `${head}\n${escapeHtml(counts)}`
    }

    case '/runs': {
      const runs = await fetchActiveRuns(op, client as never)
      if (runs.length === 0) return escapeHtml(`No recent runs (${_scope(op)}).`)
      const head = `<b>Recent runs (${escapeHtml(_scope(op))}): ${runs.length}</b>`
      const list = runs
        .slice(0, 10)
        .map((r) => `• ${escapeHtml(r.run_id.slice(0, 8))} — ${escapeHtml(r.status)}`)
        .join('\n')
      return `${head}\n${list}`
    }

    default:
      return escapeHtml(`Unknown command ${parsed.cmd}.\n\n${HELP}`)
  }
}
