/**
 * VT-297 — Telegram mutating actions (/ack, /resolve) — IDOR-safe by construction.
 *
 * A VTR references an escalation by the id shown (tap-to-copy <code>) in /alerts. We do NOT trust any
 * chat-supplied tenant/id: we resolve the reference ONLY within the operator's OWN scoped escalation set
 * (fetchEscalations already filters to assigned tenants, fail-closed) and act on the matched row's
 * SERVER-resolved id + tenant_id via actOnEscalation. So a VTR can only ever act on an escalation
 * already visible to them — pairing a foreign id with an assigned tenant is impossible (the
 * candidate set never contains foreign rows). Every action audits to ops_audit (inside
 * actOnEscalation). This is the bot analogue of the VT-293/294 server-side-resolve rule.
 */

import { actOnEscalation, fetchEscalations, type EscalationAction } from '@/lib/ops/escalations'
import { OperatorRole } from '@/lib/auth/roles'
import { escapeHtml } from '@/lib/telegram/send'

type Client = { from: (t: string) => any }

export interface BotOperator {
  operatorId: string
  role: OperatorRole
  assignedTenants: string[] | null
}

/** Apply an action to the escalation matching `reference` WITHIN the operator's scoped set.
 *  Returns an HTML-safe reply. Never trusts a chat-supplied tenant/id. */
export async function actByReference(
  operator: BotOperator,
  reference: string,
  action: EscalationAction,
  client?: Client,
): Promise<string> {
  const ref = (reference ?? '').trim()
  if (!ref) return escapeHtml(`Usage: /${action} <escalation-id> (tap-to-copy from /alerts)`)

  // Candidate set is ALREADY scoped to the operator's assigned tenants (fail-closed []).
  const rows = await fetchEscalations(operator, client as never)
  const matches = rows.filter((r) => r.reference.toLowerCase() === ref.toLowerCase())

  if (matches.length === 0) {
    // Either not theirs or not open — same generic reply (don't leak existence).
    return escapeHtml(`No open escalation ${ref} in your businesses.`)
  }
  // VT-360 (fork A): the old "ambiguous match" guard died with the REF# short-form — `reference` is
  // now the unique escalation_id, so a match set is always 0 or 1. One fewer error UX, strictly safer.

  const row = matches[0]!
  // tenant_id + id are SERVER-resolved from the scoped row — not chat input.
  const res = await actOnEscalation(operator, row.id, row.tenant_id, action, 'via telegram', client as never)
  if (!res.ok) return escapeHtml(`Couldn't ${action} ${ref}: ${res.reason ?? 'error'}`)
  return escapeHtml(`${action === 'ack' ? 'Acknowledged' : 'Resolved'} ${ref} ✓`)
}
