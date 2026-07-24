/**
 * VT-704 — activity-flow data layer: scoping (fail-closed), the eight-source union,
 * normalization, ordering, truncation honesty, day grouping.
 */
import { describe, expect, it } from 'vitest'

import { fetchTenantFlow, groupByDay, type FlowEvent } from '@/lib/ops/activity-flow'

const TID = 'aaaaaaaa-0000-0000-0000-000000000001'

function mockClient(rowsByTable: Record<string, any[]>) {
  return {
    from(table: string) {
      const chain: any = {
        _table: table,
        select() {
          return chain
        },
        eq() {
          return chain
        },
        gte() {
          return chain
        },
        not() {
          return chain
        },
        order() {
          return chain
        },
        limit() {
          return Promise.resolve({ data: rowsByTable[table] ?? [] })
        },
      }
      return chain
    },
  }
}

const VTADMIN = { role: 'vtadmin' as any, assignedTenants: null }

describe('fetchTenantFlow scoping', () => {
  it('fail-closes on an empty assigned set', async () => {
    const out = await fetchTenantFlow(
      { role: 'vtr' as any, assignedTenants: [] },
      TID,
      { client: mockClient({}) },
    )
    expect(out.events).toEqual([])
  })

  it('denies an unassigned tenant', async () => {
    const out = await fetchTenantFlow(
      { role: 'vtr' as any, assignedTenants: ['other-tenant'] },
      TID,
      { client: mockClient({ conversation_log: [{ role: 'owner', text: 'hi', created_at: '2026-07-01T00:00:00Z' }] }) },
    )
    expect(out.events).toEqual([])
  })
})

describe('fetchTenantFlow union + normalization', () => {
  it('merges all sources time-ordered with lanes, kinds and severities', async () => {
    const client = mockClient({
      conversation_log: [
        { role: 'owner', text: 'Hi', surface: 'journey', created_at: '2026-07-20T10:00:00Z' },
        { role: 'assistant', text: 'Welcome!', surface: 'journey', created_at: '2026-07-20T10:00:30Z' },
      ],
      tm_audit_log: [
        {
          event_layer: 'manager', event_kind: 'DECIDES', actor: 'manager',
          summary: 'route to SR', decision: 'delegate', action: 'dispatch', result: 'ok',
          severity: 'info', status: 'done', run_id: 'r1', created_at: '2026-07-20T10:01:00Z',
        },
      ],
      manager_tasks: [
        {
          objective: 'recover lapsed', assigned_function: 'sales_recovery', status: 'completed',
          terminal_outcome: 'success', created_at: '2026-07-20T10:02:00Z', completed_at: '2026-07-20T10:05:00Z',
        },
      ],
      pipeline_steps: [
        { step_name: 'llm_call', step_kind: 'llm', error: 'timeout', status: 'failed', started_at: '2026-07-20T10:03:00Z', run_id: 'r1' },
      ],
      pending_approvals: [
        {
          approval_type: 'campaign_send', summary: 'send 8 msgs', status: 'approved', decision: 'approved',
          requested_at: '2026-07-20T10:04:00Z', resolved_at: '2026-07-20T10:06:00Z',
        },
      ],
      owner_comms_queue: [
        { kind: 'notice', status: 'dropped', dropped_reason: 'stale_journey_push', queued_at: '2026-07-20T10:07:00Z', delivered_at: null, payload: { text_en: 'x' } },
      ],
      incidents: [
        { incident_kind: 'send_failure', severity: 'high', status: 'open', detail: 'boom', created_at: '2026-07-20T10:08:00Z' },
      ],
      tenant_alerts: [
        { trigger_kind: 'spend_spike', severity: 'medium', message_text: 'watch this', fired_at: '2026-07-20T10:09:00Z' },
      ],
    })
    const out = await fetchTenantFlow(VTADMIN, TID, { client })
    const kinds = out.events.map((e) => e.kind)
    expect(kinds).toEqual([
      'message', 'message', 'decision', 'task', 'step_error', 'approval', 'approval', 'comms', 'incident', 'alert',
    ])
    const ts = out.events.map((e) => e.ts)
    expect([...ts].sort()).toEqual(ts) // ascending

    const owner = out.events[0]!
    expect(owner.lane).toBe('owner')
    const decision = out.events[2]!
    expect(decision.body).toContain('decided: delegate')
    expect(decision.body).toContain('did: dispatch')
    const stepErr = out.events[4]!
    expect(stepErr.severity).toBe('error')
    const comms = out.events[7]!
    expect(comms.severity).toBe('warn')
    expect(comms.title).toContain('stale_journey_push')
    const incident = out.events[8]!
    expect(incident.severity).toBe('error') // high → error
  })

  it('reports per-source counts with caps (truncation honesty)', async () => {
    const out = await fetchTenantFlow(VTADMIN, TID, { client: mockClient({}) })
    expect(out.counts.conversation_log).toEqual({ fetched: 0, cap: 600 })
    expect(out.counts.tm_audit_log!.cap).toBe(500)
  })
})

describe('groupByDay', () => {
  it('groups a sorted stream into day buckets preserving order', () => {
    const ev = (ts: string): FlowEvent => ({
      ts, lane: 'system', kind: 'alert', title: 't', body: '', severity: 'info', meta: {},
    })
    const days = groupByDay([
      ev('2026-07-19T23:59:00Z'), ev('2026-07-20T00:01:00Z'), ev('2026-07-20T09:00:00Z'),
    ])
    expect(days.map((d) => d.day)).toEqual(['2026-07-19', '2026-07-20'])
    expect(days[1]!.events).toHaveLength(2)
  })
})
