/**
 * VT-515 — Debug/Failures feed: filter-matching + summary composition.
 *
 * Tests:
 *   1. _matchesDebugFilters — all filter fields, null tenant_id (pre-tenant events).
 *   2. composeEventSummary  — all composition paths (impact present, fallback to
 *      error_message, bare prefix).
 */

import { describe, expect, it } from 'vitest'

import { _matchesDebugFilters, _matchesTmAuditFilters, type DebugEvent, type TmAuditEvent } from '@/lib/ops/stream'
import { composeEventSummary } from '@/lib/ops/debug-events'
import { composeTmAuditSummary } from '@/lib/ops/tm-audit-events'

// ─── Fixtures ─────────────────────────────────────────────────────────────────

function evt(overrides: Partial<DebugEvent> = {}): DebugEvent {
  return {
    id: 'test-id',
    created_at: '2026-06-30T10:00:00Z',
    tenant_id: 'tenant-1',
    trace_id: 'trace-abc',
    failure_type: 'timeout',
    component: 'discovery',
    operation: 'knowyourgst',
    error_message: null,
    error_stack: null,
    context: null,
    severity: 'error',
    impact: null,
    vendor: null,
    vendor_status: null,
    latency_ms: null,
    ...overrides,
  }
}

// ─── _matchesDebugFilters ─────────────────────────────────────────────────────

describe('VT-515 — _matchesDebugFilters', () => {
  it('empty filters → matches everything', () => {
    expect(_matchesDebugFilters(evt(), {})).toBe(true)
    expect(_matchesDebugFilters(evt({ tenant_id: null }), {})).toBe(true)
  })

  it('tenant_id filter — exact match passes', () => {
    expect(_matchesDebugFilters(evt({ tenant_id: 'tenant-1' }), { tenant_id: 'tenant-1' })).toBe(true)
  })

  it('tenant_id filter — mismatch fails', () => {
    expect(_matchesDebugFilters(evt({ tenant_id: 'tenant-2' }), { tenant_id: 'tenant-1' })).toBe(false)
  })

  it('tenant_id filter — null tenant_id fails (pre-tenant event excluded by a specific tenant filter)', () => {
    // A null tenant_id (pre-tenant signup failure) does NOT match a specific tenant_id filter.
    expect(_matchesDebugFilters(evt({ tenant_id: null }), { tenant_id: 'tenant-1' })).toBe(false)
  })

  it('no tenant_id filter → null tenant_id still passes (pre-tenant events visible)', () => {
    expect(_matchesDebugFilters(evt({ tenant_id: null }), {})).toBe(true)
  })

  it('component filter — exact match passes', () => {
    expect(_matchesDebugFilters(evt({ component: 'create' }), { component: 'create' })).toBe(true)
  })

  it('component filter — mismatch fails', () => {
    expect(_matchesDebugFilters(evt({ component: 'discovery' }), { component: 'create' })).toBe(false)
  })

  it('severity filter — match passes', () => {
    expect(_matchesDebugFilters(evt({ severity: 'critical' }), { severity: 'critical' })).toBe(true)
  })

  it('severity filter — mismatch fails', () => {
    expect(_matchesDebugFilters(evt({ severity: 'warning' }), { severity: 'critical' })).toBe(false)
  })

  it('all three filters must pass simultaneously', () => {
    const e = evt({ tenant_id: 't1', component: 'send', severity: 'error' })
    expect(_matchesDebugFilters(e, { tenant_id: 't1', component: 'send', severity: 'error' })).toBe(true)
    // One mismatch → fail
    expect(_matchesDebugFilters(e, { tenant_id: 't1', component: 'send', severity: 'warning' })).toBe(false)
    expect(_matchesDebugFilters(e, { tenant_id: 't1', component: 'discovery', severity: 'error' })).toBe(false)
  })
})

// ─── composeEventSummary ──────────────────────────────────────────────────────

describe('VT-515 — composeEventSummary', () => {
  it('full path: component · operation · failure_type → impact', () => {
    const summary = composeEventSummary({
      component: 'discovery',
      operation: 'knowyourgst',
      failure_type: 'timeout',
      impact: 'degraded to manual',
      error_message: null,
    })
    expect(summary).toBe('discovery · knowyourgst · timeout → degraded to manual')
  })

  it('failure_type underscores replaced with spaces', () => {
    const summary = composeEventSummary({
      component: 'send',
      operation: null,
      failure_type: 'vendor_error',
      impact: null,
      error_message: null,
    })
    // No operation; failure_type underscores → spaces; no impact → bare prefix
    expect(summary).toBe('send · vendor error')
  })

  it('no operation: component · failure_type → impact', () => {
    const summary = composeEventSummary({
      component: 'create',
      operation: null,
      failure_type: 'validation',
      impact: 'blocked signup',
      error_message: null,
    })
    expect(summary).toBe('create · validation → blocked signup')
  })

  it('no impact → falls back to error_message (truncated at 80 chars)', () => {
    const longMsg = 'x'.repeat(100)
    const summary = composeEventSummary({
      component: 'ingest',
      operation: 'fetch',
      failure_type: 'network',
      impact: null,
      error_message: longMsg,
    })
    // prefix = "ingest · fetch · network", then ": " + first 80 chars
    expect(summary).toBe(`ingest · fetch · network: ${'x'.repeat(80)}`)
  })

  it('no impact, no error_message → bare prefix', () => {
    const summary = composeEventSummary({
      component: 'classify',
      operation: 'intent',
      failure_type: 'crash',
      impact: null,
      error_message: null,
    })
    expect(summary).toBe('classify · intent · crash')
  })

  it('no operation and no impact and no error_message → minimal prefix', () => {
    const summary = composeEventSummary({
      component: 'send',
      operation: null,
      failure_type: 'silent_degrade',
      impact: null,
      error_message: null,
    })
    expect(summary).toBe('send · silent degrade')
  })
})

// ─── VT-516: tm_audit_log fixtures ──────────────────────────────────────────────

function tmEvt(overrides: Partial<TmAuditEvent> = {}): TmAuditEvent {
  return {
    id: 'tm-id',
    created_at: '2026-06-30T10:00:00Z',
    tenant_id: 'tenant-1',
    run_id: 'run-1',
    trace_id: 'run-1',
    snapshot_id: null,
    event_layer: 'decides',
    event_kind: 'route_decided',
    actor: 'team_manager',
    summary: 'routed to sales_recovery',
    input: null,
    decision: null,
    reasoning_ref: null,
    action: null,
    result: null,
    severity: 'info',
    status: 'ok',
    parent_audit_id: null,
    ...overrides,
  }
}

// ─── _matchesTmAuditFilters ──────────────────────────────────────────────────────

describe('VT-516 — _matchesTmAuditFilters', () => {
  it('empty filters → matches everything', () => {
    expect(_matchesTmAuditFilters(tmEvt(), {})).toBe(true)
  })

  it('tenant_id filter — exact match passes, mismatch fails', () => {
    expect(_matchesTmAuditFilters(tmEvt({ tenant_id: 't1' }), { tenant_id: 't1' })).toBe(true)
    expect(_matchesTmAuditFilters(tmEvt({ tenant_id: 't2' }), { tenant_id: 't1' })).toBe(false)
  })

  it('event_layer filter — match passes, mismatch fails', () => {
    expect(_matchesTmAuditFilters(tmEvt({ event_layer: 'does' }), { event_layer: 'does' })).toBe(true)
    expect(_matchesTmAuditFilters(tmEvt({ event_layer: 'knows' }), { event_layer: 'does' })).toBe(false)
  })

  it('event_kind filter — match passes, mismatch fails', () => {
    expect(_matchesTmAuditFilters(tmEvt({ event_kind: 'send_result' }), { event_kind: 'send_result' })).toBe(true)
    expect(_matchesTmAuditFilters(tmEvt({ event_kind: 'spawn' }), { event_kind: 'send_result' })).toBe(false)
  })

  it('severity filter — match passes, mismatch fails', () => {
    expect(_matchesTmAuditFilters(tmEvt({ severity: 'critical' }), { severity: 'critical' })).toBe(true)
    expect(_matchesTmAuditFilters(tmEvt({ severity: 'info' }), { severity: 'critical' })).toBe(false)
  })

  it('run_id filter — match passes, mismatch fails', () => {
    expect(_matchesTmAuditFilters(tmEvt({ run_id: 'r1' }), { run_id: 'r1' })).toBe(true)
    expect(_matchesTmAuditFilters(tmEvt({ run_id: 'r2' }), { run_id: 'r1' })).toBe(false)
  })

  it('all filters must pass simultaneously', () => {
    const e = tmEvt({ tenant_id: 't1', event_layer: 'does', event_kind: 'send_result', severity: 'error', run_id: 'r1' })
    expect(
      _matchesTmAuditFilters(e, {
        tenant_id: 't1',
        event_layer: 'does',
        event_kind: 'send_result',
        severity: 'error',
        run_id: 'r1',
      }),
    ).toBe(true)
    // One mismatch → fail
    expect(_matchesTmAuditFilters(e, { tenant_id: 't1', event_layer: 'knows' })).toBe(false)
    expect(_matchesTmAuditFilters(e, { severity: 'critical' })).toBe(false)
  })
})

// ─── composeTmAuditSummary ────────────────────────────────────────────────────────

describe('VT-516 — composeTmAuditSummary', () => {
  it('with summary: actor · layer.kind → summary', () => {
    expect(
      composeTmAuditSummary({
        actor: 'team_manager',
        event_layer: 'decides',
        event_kind: 'route_decided',
        summary: 'routed to sales_recovery',
      }),
    ).toBe('team_manager · decides.route_decided → routed to sales_recovery')
  })

  it('no summary → bare prefix', () => {
    expect(
      composeTmAuditSummary({
        actor: 'integration',
        event_layer: 'gets',
        event_kind: 'retrieval',
        summary: null,
      }),
    ).toBe('integration · gets.retrieval')
  })
})
