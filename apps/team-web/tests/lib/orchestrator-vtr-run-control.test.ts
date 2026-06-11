/**
 * VT-375 Phase B — run-control read client fns (team-web → orchestrator).
 *
 * Pins the load-bearing contract for the two new fns added by B2:
 *   vtrPrograms   — programs projection for a tenant; GET on vtrRcGet helper
 *                   (X-Internal-Secret + short-lived X-Operator-Jwt, NO body).
 *   vtrRunTimeline — per-run step timeline; same GET helper, run_id in URL path.
 *
 * Contract invariants under test (mirroring orchestrator-vtr-console.test.ts style):
 *   - GET requests with X-Internal-Secret + SHORT-LIVED X-Operator-Jwt (300 s TTL).
 *   - operator_id encoded in the JWT claim (operator_id + operator_claim:true).
 *   - NO body (GET; tenant_id and run_id travel in the URL path, not the body).
 *   - URL paths: ops/run-control/programs/{tenantId}, ops/run-control/timeline/{runId}.
 *   - vtrPrograms happy path: ok=true + flat past/running/upcoming7d/holds/degraded.
 *   - vtrPrograms degraded=true when the response body carries degraded:true.
 *   - vtrPrograms fail-closed: non-2xx / throw → ok=false, empty groups, degraded=true.
 *   - vtrRunTimeline happy path: ok=true + steps + runId + tenantId + activeControls.
 *   - vtrRunTimeline fail-closed: non-2xx / throw → ok=false, steps=[], activeControls=[].
 *   - 403 → reason: 'http_403'; 404 → reason: 'http_404' (mapped via r.reason, not bespoke).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const ORIGINALS = {
  url: process.env.TEAM_ORCHESTRATOR_URL,
  secret: process.env.INTERNAL_API_SECRET,
  jwt: process.env.OPERATOR_JWT_SECRET,
}

const OP = 'operator-uuid-vt375'

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

function callArgs(f: ReturnType<typeof vi.fn>, i = 0): { url: string; opts: RequestInit } {
  const [url, opts] = f.mock.calls[i] as unknown as [string, RequestInit]
  return { url, opts }
}

function jwtPayload(token: string): Record<string, number | string | boolean> {
  const part = token.split('.')[1] ?? ''
  return JSON.parse(Buffer.from(part, 'base64url').toString('utf8'))
}

// ---------------------------------------------------------------------------
// JWT-bearing GET template — analogous to the POST template in VT-370, but
// GET has NO body: tenant/run travel in the URL path; operator identity is
// conveyed solely via the short-lived JWT claim.
// ---------------------------------------------------------------------------

describe('VT-375 — both fns use GET with X-Internal-Secret + short-lived X-Operator-Jwt', () => {
  it('sends correct headers + 300 s TTL JWT for vtrPrograms', async () => {
    const c = await client()
    const f = stub200({})
    await c.vtrPrograms(OP, 'tenant-abc')

    const { url, opts } = callArgs(f)
    expect(url).toBe('http://orch:8001/api/orchestrator/ops/run-control/programs/tenant-abc')
    expect((opts as { method?: string }).method).toBe('GET')
    expect(opts.body).toBeUndefined()

    const headers = opts.headers as Record<string, string>
    expect(headers['X-Internal-Secret']).toBe('sek')
    const token = headers['X-Operator-Jwt'] ?? ''
    expect(token).toBeTruthy()
    const payload = jwtPayload(token)
    expect(payload.operator_id).toBe(OP)
    expect(payload.operator_claim).toBe(true)
    // Mandate: OPERATOR_RESOLVE_TTL_SEC = 300 s, NEVER the 7-day default.
    expect(Number(payload.exp) - Number(payload.iat)).toBe(300)
  })

  it('sends correct headers + 300 s TTL JWT for vtrRunTimeline', async () => {
    const c = await client()
    const f = stub200({})
    await c.vtrRunTimeline(OP, 'run-uuid-7')

    const { url, opts } = callArgs(f)
    expect(url).toBe('http://orch:8001/api/orchestrator/ops/run-control/timeline/run-uuid-7')
    expect((opts as { method?: string }).method).toBe('GET')
    expect(opts.body).toBeUndefined()

    const headers = opts.headers as Record<string, string>
    expect(headers['X-Internal-Secret']).toBe('sek')
    const token = headers['X-Operator-Jwt'] ?? ''
    expect(token).toBeTruthy()
    const payload = jwtPayload(token)
    expect(payload.operator_id).toBe(OP)
    expect(payload.operator_claim).toBe(true)
    expect(Number(payload.exp) - Number(payload.iat)).toBe(300)
  })
})

// ---------------------------------------------------------------------------
// vtrPrograms
// ---------------------------------------------------------------------------

describe('VT-375 — vtrPrograms happy path', () => {
  it('returns ok=true + flat past/running/upcoming7d/holds/degraded on 200', async () => {
    const c = await client()
    stub200({
      past: [{ run_id: 'r1', run_type: 'brain', status: 'completed', started_at: 'ts', ended_at: 'ts', rerun_of_run_id: null, rerun_from_step: null, step_count: 3 }],
      running: [],
      upcoming_7d: [{ kind: 'trial_sweep', due_at: 'ts', label: 'Trial expiry', source: 'trial.yaml forecast' }],
      holds: [],
      degraded: false,
    })
    const out = await c.vtrPrograms(OP, 't1')
    expect(out.ok).toBe(true)
    expect(out.past).toHaveLength(1)
    expect(out.past[0]!.run_id).toBe('r1')
    expect(out.running).toEqual([])
    expect(out.upcoming7d).toHaveLength(1)
    expect(out.upcoming7d[0]!.kind).toBe('trial_sweep')
    expect(out.holds).toEqual([])
    expect(out.degraded).toBe(false)
    expect(out.reason).toBe('ok')
  })

  it('surfaces degraded=true from the response body (panel must show unverifiable banner)', async () => {
    const c = await client()
    stub200({ past: [], running: [], upcoming_7d: [], holds: [], degraded: true })
    const out = await c.vtrPrograms(OP, 't2')
    expect(out.ok).toBe(true)
    expect(out.degraded).toBe(true)
  })

  it('returns empty arrays when the server omits optional fields', async () => {
    const c = await client()
    stub200({})
    const out = await c.vtrPrograms(OP, 't3')
    expect(out.ok).toBe(true)
    expect(out.past).toEqual([])
    expect(out.running).toEqual([])
    expect(out.upcoming7d).toEqual([])
    expect(out.holds).toEqual([])
  })
})

describe('VT-375 — vtrPrograms fail-closed', () => {
  it('non-2xx → ok=false, empty groups, degraded=true', async () => {
    const c = await client()
    stubStatus(500)
    const out = await c.vtrPrograms(OP, 't1')
    expect(out.ok).toBe(false)
    expect(out.past).toEqual([])
    expect(out.running).toEqual([])
    expect(out.upcoming7d).toEqual([])
    expect(out.holds).toEqual([])
    // degraded=true so the canvas surfaces the unverifiable banner, never "not paused"
    expect(out.degraded).toBe(true)
  })

  it('403 → ok=false, reason contains 403', async () => {
    const c = await client()
    stubStatus(403)
    const out = await c.vtrPrograms(OP, 't1')
    expect(out.ok).toBe(false)
    expect(out.reason).toContain('403')
  })

  it('404 → ok=false, reason contains 404', async () => {
    const c = await client()
    stubStatus(404)
    const out = await c.vtrPrograms(OP, 't1')
    expect(out.ok).toBe(false)
    expect(out.reason).toContain('404')
  })

  it('network throw → ok=false, reason="error", degraded=true', async () => {
    const c = await client()
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    const out = await c.vtrPrograms(OP, 't1')
    expect(out.ok).toBe(false)
    expect(out.reason).toBe('error')
    expect(out.degraded).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// vtrRunTimeline
// ---------------------------------------------------------------------------

describe('VT-375 — vtrRunTimeline happy path', () => {
  it('returns ok=true + steps + runId + tenantId + activeControls on 200', async () => {
    const c = await client()
    const steps = [
      {
        run_id: 'r1', run_type: 'brain', run_status: 'completed', run_started_at: 'ts',
        run_ended_at: 'ts', rerun_of_run_id: null, rerun_from_step: null,
        step_id: 's1', step_seq: 1, step_kind: 'discover', step_name: 'discover',
        step_status: 'completed', started_at: 'ts', ended_at: 'ts', duration_ms: 120,
        override_id: null, paused_ms: 0, input_envelope: ['query'], output_envelope: ['results_count'],
      },
      {
        run_id: 'r1', run_type: 'brain', run_status: 'completed', run_started_at: 'ts',
        run_ended_at: 'ts', rerun_of_run_id: null, rerun_from_step: null,
        step_id: 's2', step_seq: 2, step_kind: 'brain_turn', step_name: 'brain_turn',
        step_status: 'completed', started_at: 'ts', ended_at: 'ts', duration_ms: 3200,
        override_id: null, paused_ms: 0, input_envelope: ['think_text'], output_envelope: [],
      },
    ]
    stub200({ run_id: 'r1', tenant_id: 't1', steps, active_controls: [] })
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(true)
    expect(out.runId).toBe('r1')
    expect(out.tenantId).toBe('t1')
    expect(out.steps).toHaveLength(2)
    expect(out.steps[0]!.step_kind).toBe('discover')
    expect(out.steps[1]!.step_kind).toBe('brain_turn')
    expect(out.activeControls).toEqual([])
    expect(out.reason).toBe('ok')
  })

  it('passes the per-step tier through (drives the observed/controllable badge axis)', async () => {
    const c = await client()
    const steps = [
      { run_id: 'r1', step_seq: 1, step_kind: 'discover', step_name: 'discover', step_status: 'completed', tier: 'observed' },
      { run_id: 'r1', step_seq: 2, step_kind: 'brain_turn', step_name: 'brain_turn', step_status: 'completed', tier: 'controllable' },
    ]
    stub200({ run_id: 'r1', tenant_id: 't1', steps, active_controls: [] })
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(true)
    expect(out.steps[0]!.tier).toBe('observed')
    expect(out.steps[1]!.tier).toBe('controllable')
  })

  it('surfaces activeControls when the server returns them', async () => {
    const c = await client()
    const controls = [{ tenant_id: 't1', workflow_kind: 'brain', set_at: 'ts', released_at: null }]
    stub200({ run_id: 'r1', tenant_id: 't1', steps: [], active_controls: controls })
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(true)
    expect(out.activeControls).toHaveLength(1)
    expect(out.activeControls[0]!.workflow_kind).toBe('brain')
  })

  it('returns empty steps when the server omits the steps field', async () => {
    const c = await client()
    stub200({ run_id: 'r1', tenant_id: 't1' })
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(true)
    expect(out.steps).toEqual([])
    expect(out.activeControls).toEqual([])
  })
})

describe('VT-375 — vtrRunTimeline fail-closed', () => {
  it('non-2xx → ok=false, steps=[], activeControls=[], runId=null', async () => {
    const c = await client()
    stubStatus(500)
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(false)
    expect(out.steps).toEqual([])
    expect(out.activeControls).toEqual([])
    expect(out.runId).toBeNull()
    expect(out.tenantId).toBeNull()
  })

  it('403 → ok=false, reason contains 403', async () => {
    const c = await client()
    stubStatus(403)
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(false)
    expect(out.reason).toContain('403')
  })

  it('404 → ok=false, reason contains 404', async () => {
    const c = await client()
    stubStatus(404)
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(false)
    expect(out.reason).toContain('404')
  })

  it('network throw → ok=false, reason="error", steps=[]', async () => {
    const c = await client()
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('net') }))
    const out = await c.vtrRunTimeline(OP, 'r1')
    expect(out.ok).toBe(false)
    expect(out.reason).toBe('error')
    expect(out.steps).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Component page test — no established pattern exists for ops pages.
//
// The tests/components/ pattern tests PURE FUNCTIONS exported from component
// files (foundingDisplayState, socialProofState, etc.). Ops pages (escalations,
// activity, fleet, monitoring) are Next.js server components that do NOT export
// testable pure fns — they fetch+render inline. The run-control page (B2) will
// follow the same pattern. There is NO established vitest component harness for
// ops pages in this suite; adding one here would invent a harness, which the
// build-contract explicitly forbids. Component-level verification for Phase B
// is covered by the rendered-output + console-error-free gate (VT-372 standard)
// that the integrator (CC main) runs.
// ---------------------------------------------------------------------------
