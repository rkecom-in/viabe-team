/** VT-293 — Activity: run scoping + step-stream authz + action authz (fail-closed). */

import { describe, expect, it, vi } from 'vitest'

import { OperatorRole } from '@/lib/auth/roles'
import { escalateRun, fetchActiveRuns, fetchRunSteps, flagRunControl } from '@/lib/ops/activity'

function runsClient(runs: any[]) {
  const chain: any = {
    select: () => chain, gte: () => chain, order: () => chain, limit: () => chain, in: () => chain,
    then: (r: any) => r({ data: runs }),
  }
  return { from: () => chain }
}

describe('VT-293 — fetchActiveRuns scoping', () => {
  it('VTR no assignments → [] (fail-closed)', async () => {
    const out = await fetchActiveRuns({ role: OperatorRole.VTR, assignedTenants: [] }, runsClient([{ id: 'r1', tenant_id: 't1' }]) as never)
    expect(out).toEqual([])
  })
  it('maps runs', async () => {
    const out = await fetchActiveRuns(
      { role: OperatorRole.VTADMIN, assignedTenants: null },
      runsClient([{ id: 'r1', tenant_id: 't1', status: 'running', started_at: 'now' }]) as never,
    )
    expect(out[0]!.run_id).toBe('r1')
  })
})

describe('VT-293 — fetchRunSteps authz (fail-closed)', () => {
  function client(runTenant: string, steps: any[]) {
    return {
      from: (table: string) => {
        if (table === 'pipeline_runs') {
          return { select: () => ({ eq: () => ({ limit: async () => ({ data: [{ tenant_id: runTenant }] }) }) }) }
        }
        return { select: () => ({ eq: () => ({ order: async () => ({ data: steps }) }) }) }
      },
    }
  }
  it('VTR not assigned to run tenant → [] (no step leak)', async () => {
    const out = await fetchRunSteps(
      { role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'run1',
      client('tz', [{ step_index: 0, step_kind: 'x' }]) as never,
    )
    expect(out).toEqual([])
  })
  it('VTR assigned → steps returned', async () => {
    const out = await fetchRunSteps(
      { role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'run1',
      client('ta', [{ step_index: 0, step_kind: 'classify', status: 'ok', started_at: 'now', duration_ms: 12, rationale: 'r' }]) as never,
    )
    expect(out).toHaveLength(1)
    expect(out[0]!.step_kind).toBe('classify')
  })
})

describe('VT-293 — escalateRun authz + writes (IDOR-hardened: tenant resolved from run)', () => {
  // client resolves the run's TRUE tenant from pipeline_runs; the caller passes NO tenant.
  function actionClient(runTenant: string | null) {
    const upsert = vi.fn(async () => ({ error: null }))
    const insert = vi.fn(async (_r: Record<string, unknown>) => ({ error: null }))
    const client = {
      from: (t: string) => {
        if (t === 'pipeline_runs') {
          return { select: () => ({ eq: () => ({ limit: async () => ({ data: runTenant ? [{ tenant_id: runTenant }] : [] }) }) }) }
        }
        return t === 'escalations' ? { upsert } : { insert }
      },
    }
    return { client, upsert, insert }
  }

  it('IDOR: run belongs to an UNASSIGNED tenant → rejected, no writes (cannot spoof)', async () => {
    const { client, upsert, insert } = actionClient('tz') // run's real tenant = tz
    const res = await escalateRun({ operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'r1', client as never)
    expect(res.ok).toBe(false)
    expect(upsert).not.toHaveBeenCalled()
    expect(insert).not.toHaveBeenCalled()
  })
  it('run not found → rejected', async () => {
    const { client } = actionClient(null)
    const res = await escalateRun({ operatorId: 'op', role: OperatorRole.VTADMIN, assignedTenants: null }, 'rX', client as never)
    expect(res.ok).toBe(false)
    expect(res.reason).toBe('run not found')
  })
  it('assigned (run tenant in set) → escalation upsert + ops_audit insert w/ resolved tenant', async () => {
    const { client, upsert, insert } = actionClient('ta') // run's real tenant = ta
    const res = await escalateRun({ operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'r1', client as never)
    expect(res.ok).toBe(true)
    expect(upsert).toHaveBeenCalledTimes(1)
    expect(insert).toHaveBeenCalledTimes(1)
    expect(insert.mock.calls[0]![0].action).toBe('escalate')
    expect(insert.mock.calls[0]![0].tenant_id).toBe('ta') // resolved tenant written
  })
})

describe('VT-300 — flagRunControl forwards to the authoritative orchestrator endpoint', () => {
  function runClient(runTenant: string | null) {
    return {
      from: (t: string) => {
        if (t === 'pipeline_runs') {
          return { select: () => ({ eq: () => ({ limit: async () => ({ data: runTenant ? [{ tenant_id: runTenant }] : [] }) }) }) }
        }
        return { insert: async () => ({ error: null }) }
      },
    }
  }

  it('unassigned tenant → rejected, endpoint NOT called (fast pre-check)', async () => {
    const fwd = vi.fn(async () => ({ ok: true, reason: 'ok' }))
    const res = await flagRunControl(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'r1', 'pause',
      runClient('tz') as never, fwd as never,
    )
    expect(res.ok).toBe(false)
    expect(fwd).not.toHaveBeenCalled()
  })

  it('run not found → rejected', async () => {
    const fwd = vi.fn(async () => ({ ok: true, reason: 'ok' }))
    const res = await flagRunControl(
      { operatorId: 'op', role: OperatorRole.VTADMIN, assignedTenants: null }, 'rX', 'override',
      runClient(null) as never, fwd as never,
    )
    expect(res.ok).toBe(false)
    expect(fwd).not.toHaveBeenCalled()
  })

  it('assigned → forwards (operator_id, run_id, control_type); endpoint is authoritative', async () => {
    const fwd = vi.fn(async () => ({ ok: true, reason: 'ok' }))
    const res = await flagRunControl(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'r1', 'pause',
      runClient('ta') as never, fwd as never,
    )
    expect(res.ok).toBe(true)
    expect(fwd).toHaveBeenCalledWith('op', 'r1', 'pause')
  })

  it('endpoint 403 (orchestrator re-check denies) → surfaced', async () => {
    const fwd = vi.fn(async () => ({ ok: false, reason: 'http_403' }))
    const res = await flagRunControl(
      { operatorId: 'op', role: OperatorRole.VTR, assignedTenants: ['ta'] }, 'r1', 'steer',
      runClient('ta') as never, fwd as never,
    )
    expect(res.ok).toBe(false)
    expect(res.reason).toBe('http_403')
  })
})
