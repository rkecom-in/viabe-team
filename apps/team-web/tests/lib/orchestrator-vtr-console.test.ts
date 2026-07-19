/**
 * VT-370 Gap-6 — VTR console client fns (team-web → orchestrator).
 *
 * Pins the load-bearing contract for all seven fns:
 *   - the forwardVtrVerify template: X-Internal-Secret + X-Operator-Jwt on EVERY call
 *     (never the run-control no-JWT shape);
 *   - every JWT minted SHORT-LIVED ({ ttlSec: OPERATOR_RESOLVE_TTL_SEC } = 300s) — the bare
 *     issueOperatorJwt default is 7 days and is forbidden on this surface;
 *   - operator_id passed through into the body (the orchestrator re-verifies it == claim);
 *   - vtr-batch-cancel / vtr-batch-drafts carry NO tenant_id (server-derived, VT-293/294);
 *   - status mapping: 409 → stale_version, 400 → scrubbed violations, 403 → forbidden;
 *   - fail-closed on non-2xx / throw.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const ORIGINALS = {
  url: process.env.TEAM_ORCHESTRATOR_URL,
  secret: process.env.INTERNAL_API_SECRET,
  jwt: process.env.OPERATOR_JWT_SECRET,
}

const OP = 'operator-uuid-1'

beforeEach(() => {
  process.env.TEAM_ORCHESTRATOR_URL = 'http://orch:8001'
  process.env.INTERNAL_API_SECRET = 'sek'
  process.env.OPERATOR_JWT_SECRET = 'unit-test-operator-jwt-secret'
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  for (const [k, v] of [
    ['TEAM_ORCHESTRATOR_URL', ORIGINALS.url],
    ['INTERNAL_API_SECRET', ORIGINALS.secret],
    ['OPERATOR_JWT_SECRET', ORIGINALS.jwt],
  ] as const) {
    if (v === undefined) delete process.env[k]
    else process.env[k] = v
  }
})

async function client() {
  return await import('@/lib/orchestrator-client')
}

function stub200(body: Record<string, unknown>) {
  const f = vi.fn(async () => ({ ok: true, status: 200, json: async () => body }))
  vi.stubGlobal('fetch', f)
  return f
}

function stubStatus(status: number, body: Record<string, unknown> = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: false, status, json: async () => body })),
  )
}

function call(f: ReturnType<typeof vi.fn>, i = 0): { url: string; opts: RequestInit; body: Record<string, unknown> } {
  const [url, opts] = f.mock.calls[i] as unknown as [string, RequestInit]
  return { url, opts, body: JSON.parse(String(opts.body)) as Record<string, unknown> }
}

function jwtPayload(token: string): Record<string, number | string | boolean> {
  const part = token.split('.')[1] ?? ''
  return JSON.parse(Buffer.from(part, 'base64url').toString('utf8'))
}

describe('VT-370 — every fn rides the JWT-bearing template with the 5-min TTL', () => {
  it('sends X-Internal-Secret + a SHORT-LIVED X-Operator-Jwt naming the operator, on all 9', async () => {
    const c = await client()
    const invocations: [string, () => Promise<unknown>][] = [
      ['vtr-plan', () => c.vtrPlan(OP, 't1')],
      ['vtr-plan-edit', () => c.vtrPlanEdit(OP, 't1', 'i1', { why: 'x' }, 3)],
      ['vtr-agent-state', () => c.vtrAgentState(OP, 't1')],
      ['vtr-tenant-profile', () => c.vtrTenantProfile(OP, 't1')],
      ['vtr-confirm-field', () => c.vtrConfirmField(OP, 't1', 'about')],
      ['vtr-draft-batches', () => c.vtrDraftBatches(OP, 't1')],
      ['vtr-autonomy-override', () => c.vtrAutonomyOverride(OP, 't1', 'reputation', 'freeze', 'r')],
      ['vtr-batch-cancel', () => c.vtrBatchCancel(OP, 'b1', 'r')],
      ['vtr-batch-drafts', () => c.vtrBatchDrafts(OP, 'b1')],
    ]
    for (const [path, invoke] of invocations) {
      const f = stub200({})
      await invoke()
      const { url, opts, body } = call(f)
      expect(url).toBe(`http://orch:8001/api/orchestrator/ops/${path}`)
      const headers = opts.headers as Record<string, string>
      expect(headers['X-Internal-Secret']).toBe('sek')
      const token = headers['X-Operator-Jwt'] ?? ''
      expect(token).toBeTruthy()
      const payload = jwtPayload(token)
      expect(payload.operator_id).toBe(OP)
      expect(payload.operator_claim).toBe(true)
      // The mandate: OPERATOR_RESOLVE_TTL_SEC (300s), NEVER the 7-day default.
      expect(Number(payload.exp) - Number(payload.iat)).toBe(300)
      // operator_id passes through into the body (orchestrator asserts body == claim).
      expect(body.operator_id).toBe(OP)
    }
  })
})

describe('VT-370 — vtrPlan', () => {
  it('returns plan + history on 200', async () => {
    const c = await client()
    const plan = { tenant_id: 't1', version: 4, roadmap_json: [] }
    const history = [{ tenant_id: 't1', version: 4, generated_by: 'vtr:x', model_id: null, created_at: null }]
    const f = stub200({ plan, history })
    const out = await c.vtrPlan(OP, 't1')
    expect(out.ok).toBe(true)
    expect(out.plan).toEqual(plan)
    expect(out.history).toEqual(history)
    expect(call(f).body.tenant_id).toBe('t1')
  })

  it('fails closed (null plan, empty history) on non-2xx and on throw', async () => {
    const c = await client()
    stubStatus(403)
    expect(await c.vtrPlan(OP, 't1')).toEqual({ ok: false, plan: null, history: [], reason: 'http_403' })
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network')
    }))
    expect((await c.vtrPlan(OP, 't1')).ok).toBe(false)
  })
})

describe('VT-370 — vtrPlanEdit status mapping', () => {
  it('200 → ok + new_version; body carries patch + expected_prev_version', async () => {
    const c = await client()
    const f = stub200({ ok: true, new_version: 5 })
    const out = await c.vtrPlanEdit(OP, 't1', 'item-9', { month: 2 }, 4)
    expect(out).toEqual({ ok: true, newVersion: 5, reason: 'ok', violations: [] })
    const { body } = call(f)
    expect(body.item_id).toBe('item-9')
    expect(body.patch).toEqual({ month: 2 })
    expect(body.expected_prev_version).toBe(4)
  })

  it('409 → stale_version (optimistic-concurrency surface)', async () => {
    const c = await client()
    stubStatus(409, { detail: 'stale' })
    expect((await c.vtrPlanEdit(OP, 't1', 'i', { why: 'x' }, 1)).reason).toBe('stale_version')
  })

  it('400 → grounding_or_patch with the (server-scrubbed) violation strings', async () => {
    const c = await client()
    stubStatus(400, { detail: ['uncited number: [scrubbed]', 'bad field'] })
    const out = await c.vtrPlanEdit(OP, 't1', 'i', { why: 'x' }, 1)
    expect(out.reason).toBe('grounding_or_patch')
    expect(out.violations).toEqual(['uncited number: [scrubbed]', 'bad field'])
  })

  it('403 → forbidden; 404 → not_found', async () => {
    const c = await client()
    stubStatus(403)
    expect((await c.vtrPlanEdit(OP, 't1', 'i', { why: 'x' }, 1)).reason).toBe('forbidden')
    stubStatus(404)
    expect((await c.vtrPlanEdit(OP, 't1', 'i', { why: 'x' }, 1)).reason).toBe('not_found')
  })
})

describe('VT-370 — vtrAgentState / vtrDraftBatches reads fail closed', () => {
  it('vtrAgentState returns agents on 200, [] otherwise', async () => {
    const c = await client()
    stub200({ agents: [{ agent: 'reputation', level: 'L2' }] })
    expect((await c.vtrAgentState(OP, 't1')).agents).toHaveLength(1)
    stubStatus(500)
    expect((await c.vtrAgentState(OP, 't1')).agents).toEqual([])
  })

  it('vtrDraftBatches sends limit and returns rows + count; [] on error', async () => {
    const c = await client()
    const f = stub200({ rows: [{ batch_id: 'b1' }], count: 1 })
    const out = await c.vtrDraftBatches(OP, 't1', 50)
    expect(out.rows).toHaveLength(1)
    expect(out.count).toBe(1)
    expect(call(f).body.limit).toBe(50)
    stubStatus(502)
    expect((await c.vtrDraftBatches(OP, 't1')).rows).toEqual([])
  })
})

describe('VT-405 — vtrTenantProfile read fails closed', () => {
  it('returns the profile on 200, null otherwise', async () => {
    const c = await client()
    stub200({ profile: { tenant_id: 't1', business_name: 'Sundaram', whatsapp_last4: '3598' } })
    const ok = await c.vtrTenantProfile(OP, 't1')
    expect(ok.ok).toBe(true)
    expect(ok.profile?.business_name).toBe('Sundaram')
    expect(ok.profile?.whatsapp_last4).toBe('3598')
    stubStatus(403)
    expect(await c.vtrTenantProfile(OP, 't1')).toEqual({ ok: false, profile: null, reason: 'http_403' })
  })
})

describe('VT-405 Part B — vtrConfirmField promotes one field (value server-read)', () => {
  it('200 → ok + field + status; body carries field + basis, NOT a value', async () => {
    const c = await client()
    const f = stub200({ ok: true, field: 'about', status: 'vtr_confirmed' })
    const out = await c.vtrConfirmField(OP, 't1', 'about', 'looked legit')
    expect(out).toEqual({ ok: true, field: 'about', status: 'vtr_confirmed', reason: 'ok' })
    const { body } = call(f)
    expect(body.tenant_id).toBe('t1')
    expect(body.field).toBe('about')
    expect(body.basis).toBe('looked legit')
    // The client never sends a value — only the field NAME (PII/IDOR boundary).
    expect(body).not.toHaveProperty('value')
  })

  it('400 → invalid_field; 403 → forbidden; 404 → not_found; fails closed', async () => {
    const c = await client()
    stubStatus(400)
    expect((await c.vtrConfirmField(OP, 't1', '_field_provenance')).reason).toBe('invalid_field')
    stubStatus(403)
    expect((await c.vtrConfirmField(OP, 't1', 'about')).reason).toBe('forbidden')
    stubStatus(404)
    expect((await c.vtrConfirmField(OP, 't1', 'about')).reason).toBe('not_found')
    expect((await c.vtrConfirmField(OP, 't1', 'about')).ok).toBe(false)
  })
})

describe('VT-370 — vtrAutonomyOverride', () => {
  it('forwards tenant/agent/action/reason; returns state + batches_cancelled', async () => {
    const c = await client()
    const f = stub200({ ok: true, state: { level: 'L2', frozen: true, streak: 0 }, batches_cancelled: 2 })
    const out = await c.vtrAutonomyOverride(OP, 't1', 'reputation', 'freeze', 'why')
    expect(out.ok).toBe(true)
    expect(out.state).toEqual({ level: 'L2', frozen: true, streak: 0 })
    expect(out.batchesCancelled).toBe(2)
    const { body } = call(f)
    expect(body).toMatchObject({ tenant_id: 't1', agent: 'reputation', action: 'freeze', reason: 'why' })
  })

  it('403 → forbidden (assignment denial)', async () => {
    const c = await client()
    stubStatus(403)
    expect((await c.vtrAutonomyOverride(OP, 't1', 'reputation', 'demote', 'r')).reason).toBe('forbidden')
  })
})

describe('VT-370 — vtrBatchCancel derives tenant server-side', () => {
  it('sends batch_id + reason and NO tenant_id (VT-293/294 discipline)', async () => {
    const c = await client()
    const f = stub200({ ok: true, tenant_id: 't1', drafts_halted: 3 })
    const out = await c.vtrBatchCancel(OP, 'batch-7', 'bad batch')
    expect(out).toEqual({ ok: true, tenantId: 't1', draftsHalted: 3, reason: 'ok' })
    const { body } = call(f)
    expect(body).not.toHaveProperty('tenant_id')
    expect(body.batch_id).toBe('batch-7')
  })

  it('404 → not_found; 403 → forbidden', async () => {
    const c = await client()
    stubStatus(404)
    expect((await c.vtrBatchCancel(OP, 'b', 'r')).reason).toBe('not_found')
    stubStatus(403)
    expect((await c.vtrBatchCancel(OP, 'b', 'r')).reason).toBe('forbidden')
  })
})

describe('VT-370 — vtrBatchDrafts (exception tier)', () => {
  it('returns drafts on 200; NO tenant_id in the body', async () => {
    const c = await client()
    const drafts = [{ template_name: 'tmpl', params: { a: 1 }, status: 'drafted', skip_reason: null }]
    const f = stub200({ drafts })
    const out = await c.vtrBatchDrafts(OP, 'batch-7')
    expect(out.ok).toBe(true)
    expect(out.drafts).toEqual(drafts)
    expect(call(f).body).not.toHaveProperty('tenant_id')
  })

  it('403 (non-exception operator) → graceful forbidden with empty drafts', async () => {
    const c = await client()
    stubStatus(403, { detail: 'exception tier required' })
    expect(await c.vtrBatchDrafts(OP, 'b')).toEqual({ ok: false, drafts: [], reason: 'forbidden' })
  })
})
