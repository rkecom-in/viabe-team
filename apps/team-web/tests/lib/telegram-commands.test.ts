/** VT-297 — Telegram read-command router + reply send (escaping, scoping pass-through). */

import { describe, expect, it } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import { dispatchReadCommand, parseCommand } from '@/lib/telegram/commands'
import { clampMessage, escapeHtml } from '@/lib/telegram/send'

const VTR = (assigned: string[]) => ({ operatorId: 'op', role: OperatorRole.VTR, assignedTenants: assigned })
const ADMIN = { operatorId: 'a', role: OperatorRole.VTADMIN, assignedTenants: null }

/** Fake client feeding the underlying ops reads (escalations/monitoring/runs). */
function client(rows: any[]) {
  const chain: any = {
    select: () => chain,
    eq: () => chain,
    neq: () => chain,
    is: () => chain,
    gte: () => chain,
    order: () => chain,
    limit: () => chain,
    in: () => chain,
    then: (resolve: any) => resolve({ data: rows, error: null }),
  }
  return { from: () => chain }
}

describe('VT-297 — parseCommand', () => {
  it('parses /cmd + args', () => {
    expect(parseCommand('/status t1')).toEqual({ cmd: '/status', args: ['t1'] })
  })
  it('non-command text → empty cmd', () => {
    expect(parseCommand('hello there').cmd).toBe('')
  })
  it('lowercases the command', () => {
    expect(parseCommand('/HELP').cmd).toBe('/help')
  })
})

describe('VT-297 — escapeHtml / clamp', () => {
  it('escapes HTML-significant chars (business_name injection guard)', () => {
    expect(escapeHtml('<b>A&Co</b>')).toBe('&lt;b&gt;A&amp;Co&lt;/b&gt;')
  })
  it('clamps very long text without erroring', () => {
    const out = clampMessage('x'.repeat(5000))
    expect(out.length).toBeLessThan(5000)
    expect(out).toContain('truncated')
  })
})

describe('VT-297 — dispatchReadCommand', () => {
  it('/help → static help (no DB)', async () => {
    const out = await dispatchReadCommand(parseCommand('/help'), VTR(['t1']))
    expect(out).toContain('/alerts')
    expect(out).toContain('/status')
  })

  it('/alerts VTR-unassigned → fail-closed "No open escalations"', async () => {
    const out = await dispatchReadCommand(parseCommand('/alerts'), VTR([]), client([{ id: 'e1', tenant_id: 't9' }]) as never)
    expect(out).toContain('No open escalations')
  })

  it('/alerts → operational summary, no PII, id handle in tap-to-copy <code>', async () => {
    const out = await dispatchReadCommand(
      parseCommand('/alerts'),
      ADMIN,
      client([{ id: 'abc123de', tenant_id: 't1', kind: 'hard_limit', severity: 'high', status: 'open', opened_at: 'now' }]) as never,
    )
    // VT-360: reference is the escalation id (operational handle, F2), rendered <code> for tap-to-copy.
    expect(out).toContain('<code>abc123de</code>')
    expect(out).toContain('Escalations')
    expect(out).not.toMatch(/\+?\d{10}/) // no phone-shaped digits
  })

  it('/status → watchdog category counts', async () => {
    const out = await dispatchReadCommand(
      parseCommand('/status'),
      ADMIN,
      client([
        { id: 'a1', tenant_id: 't1', trigger_kind: 'hard_limit', severity: 'critical', fired_at: 'now', run_id: 'r1', message_text: null },
      ]) as never,
    )
    expect(out).toContain('Watchdog')
    expect(out).toContain('crash') // hard_limit → crash category
  })

  it('/runs → run list with short ids', async () => {
    const out = await dispatchReadCommand(
      parseCommand('/runs'),
      ADMIN,
      client([{ id: 'abcdef1234', tenant_id: 't1', status: 'running', started_at: 'now' }]) as never,
    )
    expect(out).toContain('Recent runs')
    expect(out).toContain('abcdef12')
  })

  it('unknown command → help hint', async () => {
    const out = await dispatchReadCommand(parseCommand('/nope'), ADMIN, client([]) as never)
    expect(out).toContain('Unknown command')
  })
})
