/**
 * VT-412 PR-D — the load-bearing security proof for opening the ops run-replay cluster to
 * scoped VTR operators. Tests the access + de-identification helpers (lib/ops/run-replay-access)
 * that gate the four pages, mirroring the VT-290 / VT-377 dep-less-module idiom (no server-
 * component harness invented — the page constraint). The four required proofs:
 *
 *   (a) a VTR ASSIGNED to tenant A can replay tenant A's run.
 *   (b) a VTR NOT assigned to tenant B is DENIED replaying tenant B's run — the IDOR boundary,
 *       reached by a direct runId guess (the tenant is server-resolved from the run row, so the
 *       guess only yields its real tenant; canReplayRun denies on the unassigned tenant).
 *   (c) VTAdmin / Fazal (assignedTenants === null) retain FULL access (replay + full read path).
 *   (d) a VTR's de-identified step carries NO raw customer name / phone — even for a synthetic
 *       record whose name is NOT pattern-matchable (the de-id is structural: the columns are
 *       dropped, not pattern-redacted).
 */

import { describe, expect, it } from 'vitest'

import type { PipelineStepRow } from '@/lib/ops/data-access'
import {
  canReplayRun,
  canUseFreeTextSearch,
  deIdentifyStepForVtr,
  envelopeKeysOnly,
  hasFullReadAccess,
  scopeHistoryTenantFilter,
} from '@/lib/ops/run-replay-access'

const TENANT_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const TENANT_B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'

// A VTR assigned ONLY to tenant A. A VTAdmin/Fazal session is assignedTenants === null.
const VTR_ASSIGNED_A: string[] = [TENANT_A]
const VTADMIN: null = null

describe('VT-412 (a) — a VTR assigned to tenant A can replay tenant A run', () => {
  it('canReplayRun → true for the assigned tenant (server-resolved)', () => {
    expect(canReplayRun(VTR_ASSIGNED_A, TENANT_A)).toBe(true)
  })
  it('the VTR reads the de-identified path (not full)', () => {
    expect(hasFullReadAccess(VTR_ASSIGNED_A)).toBe(false)
  })
})

describe('VT-412 (b) — a VTR NOT assigned to tenant B is DENIED (the IDOR boundary)', () => {
  it('canReplayRun → false: guessing a runId whose tenant is B is denied', () => {
    // The runId guess resolves SERVER-SIDE to its real tenant (B). The gate sees B ∉ {A}.
    expect(canReplayRun(VTR_ASSIGNED_A, TENANT_B)).toBe(false)
  })
  it('a null/unknown tenant (un-resolvable run) fails CLOSED', () => {
    expect(canReplayRun(VTR_ASSIGNED_A, null)).toBe(false)
  })
  it('a VTR with an EMPTY assigned set can replay nothing (fail-closed)', () => {
    expect(canReplayRun([], TENANT_A)).toBe(false)
    expect(canReplayRun([], TENANT_B)).toBe(false)
  })
  it('the history tenant filter cannot be widened past the assigned set (IDOR)', () => {
    // A VTR passing tenant_ids=[B] (not assigned) gets an empty effective filter ⇒ denied.
    const out = scopeHistoryTenantFilter(VTR_ASSIGNED_A, [TENANT_B])
    expect(out.denied).toBe(true)
    expect(out.tenantIds).toEqual([])
  })
  it('an absent history filter scopes to the assigned set, NEVER "all"', () => {
    const out = scopeHistoryTenantFilter(VTR_ASSIGNED_A, undefined)
    expect(out.denied).toBe(false)
    expect(out.tenantIds).toEqual([TENANT_A]) // never undefined (= all)
  })
  it('a mixed filter keeps ONLY the assigned tenant', () => {
    const out = scopeHistoryTenantFilter(VTR_ASSIGNED_A, [TENANT_A, TENANT_B])
    expect(out.tenantIds).toEqual([TENANT_A])
    expect(out.denied).toBe(false)
  })
})

describe('VT-412 PR-D (Finding 1) — the `q` free-text search oracle is VTAdmin-only', () => {
  // The `q` search runs against the RAW envelope tsvector (envelope_search_tsv,
  // migrations/038) BEFORE de-id, so for a VTR it would be a result-set MEMBERSHIP
  // oracle even though returned rows are de-identified. Only VTAdmin/Fazal keep `q`.
  it('a VTR (assigned set) may NOT use the q oracle', () => {
    expect(canUseFreeTextSearch(VTR_ASSIGNED_A)).toBe(false)
  })
  it('a VTR with an EMPTY assigned set still may NOT use q (fail-closed, non-null)', () => {
    expect(canUseFreeTextSearch([])).toBe(false)
  })
  it('VTAdmin / Fazal (null) KEEP q — same boundary as the full read path', () => {
    expect(canUseFreeTextSearch(VTADMIN)).toBe(true)
    // The q gate tracks the full-read gate exactly (both = "assignedTenants === null").
    expect(canUseFreeTextSearch(VTADMIN)).toBe(hasFullReadAccess(VTADMIN))
    expect(canUseFreeTextSearch(VTR_ASSIGNED_A)).toBe(hasFullReadAccess(VTR_ASSIGNED_A))
  })
})

describe('VT-412 (c) — VTAdmin / Fazal retain full access', () => {
  it('canReplayRun → true for ANY tenant (unscoped)', () => {
    expect(canReplayRun(VTADMIN, TENANT_A)).toBe(true)
    expect(canReplayRun(VTADMIN, TENANT_B)).toBe(true)
  })
  it('reads the FULL service-role path (not de-identified)', () => {
    expect(hasFullReadAccess(VTADMIN)).toBe(true)
  })
  it('the history filter passes through verbatim (unscoped — undefined stays undefined)', () => {
    expect(scopeHistoryTenantFilter(VTADMIN, undefined)).toEqual({
      tenantIds: undefined,
      denied: false,
    })
    expect(scopeHistoryTenantFilter(VTADMIN, [TENANT_B])).toEqual({
      tenantIds: [TENANT_B],
      denied: false,
    })
  })
})

describe('VT-412 (d) — de-identification: no raw customer name / phone reaches a VTR', () => {
  // A synthetic step whose envelopes + think-text carry a NON-pattern customer name
  // ("Lakshmi" — no digits/email/PAN; a pattern-only redactor would let it survive) and a
  // phone. The de-id is STRUCTURAL: decision_rationale / error / tool_calls are dropped and
  // envelopes are reduced to KEY NAMES — so the VALUES never appear regardless of pattern.
  const RAW_NAME = 'Lakshmi'
  const RAW_PHONE = '+919876543210'
  const rawStep: PipelineStepRow = {
    id: 'step-1',
    run_id: 'run-1',
    step_seq: 3,
    step_kind: 'agent_reasoning_step',
    step_name: 'decide_reply',
    parent_step_id: null,
    status: 'ok',
    decision_rationale: `Customer ${RAW_NAME} asked to reschedule; confirming on ${RAW_PHONE}.`,
    model_used: 'claude',
    tokens_input: 100,
    tokens_output: 50,
    cost_paise: 7,
    duration_ms: 1200,
    tool_calls: [{ name: 'lookup', args: { customer_name: RAW_NAME, phone: RAW_PHONE } }],
    input_envelope: { customer_name: RAW_NAME, phone: RAW_PHONE, intent: 'reschedule' },
    output_envelope: { reply_to: RAW_PHONE, greeting: `Hi ${RAW_NAME}` },
    error: { message: `failed sending to ${RAW_NAME} at ${RAW_PHONE}` },
    started_at: '2026-06-24T00:00:00Z',
    ended_at: '2026-06-24T00:00:01Z',
  }

  it('decision_rationale is DROPPED (not pattern-redacted) — the think-text never reaches a VTR', () => {
    const deid = deIdentifyStepForVtr(rawStep)
    expect(deid.decision_rationale).toBeNull()
  })

  it('error + tool_calls are DROPPED', () => {
    const deid = deIdentifyStepForVtr(rawStep)
    expect(deid.error).toBeNull()
    expect(deid.tool_calls).toBeNull()
  })

  it('envelopes are reduced to KEY NAMES only — no values', () => {
    const deid = deIdentifyStepForVtr(rawStep)
    expect(deid.input_envelope).toEqual(['customer_name', 'phone', 'intent'])
    expect(deid.output_envelope).toEqual(['reply_to', 'greeting'])
  })

  it('the serialized de-identified step contains NEITHER the raw name NOR the phone', () => {
    const deid = deIdentifyStepForVtr(rawStep)
    const blob = JSON.stringify(deid)
    expect(blob).not.toContain(RAW_NAME)
    expect(blob).not.toContain('919876543210')
    expect(blob).not.toContain(RAW_PHONE)
  })

  it('non-PII operational columns survive (the VTR still sees useful telemetry)', () => {
    const deid = deIdentifyStepForVtr(rawStep)
    expect(deid.step_kind).toBe('agent_reasoning_step')
    expect(deid.status).toBe('ok')
    expect(deid.duration_ms).toBe(1200)
    expect(deid.step_seq).toBe(3)
  })

  it('envelopeKeysOnly: null/array/scalar edge cases never leak values', () => {
    expect(envelopeKeysOnly(null)).toBeNull()
    expect(envelopeKeysOnly(undefined)).toBeNull()
    expect(envelopeKeysOnly([RAW_NAME, RAW_PHONE])).toEqual([]) // array ⇒ no keys, no values
    expect(envelopeKeysOnly('a raw string with ' + RAW_NAME)).toEqual([]) // scalar ⇒ []
  })
})
